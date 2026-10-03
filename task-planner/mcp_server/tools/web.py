"""工具 T2：fetch_webpage —— 抓取网页正文（攻略、签证政策、开放时间）。

安全约束（重要）
----------------
本工具会向**模型指定的任意 URL** 发起请求，因此必须防 SSRF：

1. **拒绝内网 / 保留地址** —— 含 127.0.0.0/8、10/8、172.16/12、192.168/16、
   169.254/16（云元数据端点！）、::1、fc00::/7 等。
   校验在 **DNS 解析之后**做，否则 ``http://evil.example`` 解析到 127.0.0.1 就能绕过。
2. **手动跟随重定向并逐跳校验** —— 否则 302 到内网即可绕过首次校验。
3. **限制下载体积** —— 流式读取，超过阈值即中止，避免一个大页面把进程打爆。

正文提取的两个坑
----------------
1. ``head`` **不能**放进跳过集合 —— ``<title>`` 在 ``head`` 内，
   把 ``head`` 整个跳过会让标题永远解析为空。
2. 跳过状态用**栈**而非计数器 —— 畸形 HTML（标签不闭合）下计数器会永久卡住，
   把后续正文全部吞掉。
"""

from __future__ import annotations

import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_BAD_ARGS, ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.tools.base import env, register, tool_timeout

USER_AGENT = "TaskPlannerBot/0.1 (+educational use)"

# 不参与正文的标签。注意 head 不在其中（title 需要保留）。
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "iframe"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
}

# 内网 / 元数据地址黑名单（主机名层面；DNS 解析后再校验 IP）
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".localdomain")

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_REDIRECTS = 5


class WebParams(BaseModel):
    url: str = Field(description="要抓取的网页地址，必须是 http/https")
    max_chars: int = Field(default=4000, ge=200, le=20000, description="正文最大字符数")

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("url 必须是合法的 http/https 地址")
        return v


# ---------------------------------------------------------------------------
# SSRF 防护
# ---------------------------------------------------------------------------


def host_is_blocked(host: str | None) -> str | None:
    """返回拒绝原因；``None`` 表示允许访问。

    设置 ``ALLOW_PRIVATE_URLS=1`` 可关闭拦截 —— **仅用于本地开发与测试**
    （例如对着本机的 mock 服务跑端到端用例）。生产环境绝不要开。
    """
    if env("ALLOW_PRIVATE_URLS") == "1":
        return None

    if not host:
        return "URL 缺少主机名"

    name = host.strip("[]").rstrip(".").lower()
    if name in BLOCKED_HOSTNAMES or name.endswith(BLOCKED_HOST_SUFFIXES):
        return f"{host} 是内网 / 元数据地址"

    try:
        infos = socket.getaddrinfo(name, None)
    except socket.gaierror:
        return None  # 解析不了就交给 httpx 报错，避免把正常域名误判为内网

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"{host} 解析到内网 / 保留地址 {ip}"
    return None


def _fetch_limited(url: str, *, timeout: int) -> tuple[str | None, str]:
    """流式抓取，逐跳校验重定向。返回 ``(html, 错误信息)``。"""
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            return None, f"不支持的协议: {parsed.scheme or '(空)'}"

        blocked = host_is_blocked(parsed.hostname)
        if blocked:
            return None, f"拒绝访问：{blocked}"

        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                with client.stream("GET", current, headers={"User-Agent": USER_AGENT}) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return None, "重定向响应缺少 Location 头"
                        # 相对跳转要基于当前 URL 解析；下一轮循环会重新校验
                        current = str(httpx.URL(current).join(location))
                        continue

                    if resp.status_code >= 400:
                        return None, f"目标站点返回 {resp.status_code}"

                    content_type = resp.headers.get("content-type", "")
                    if "html" not in content_type and "text" not in content_type:
                        return None, f"不支持的内容类型: {content_type or 'unknown'}"

                    buffer = bytearray()
                    truncated_bytes = False
                    for chunk in resp.iter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > MAX_DOWNLOAD_BYTES:
                            truncated_bytes = True
                            break

                    encoding = resp.encoding or "utf-8"
                    html = bytes(buffer).decode(encoding, errors="replace")
                    if truncated_bytes:
                        limit_mb = MAX_DOWNLOAD_BYTES // 1024 // 1024
                        html += f"\n<!-- 内容超过 {limit_mb}MB，已截断 -->"
                    return html, ""
        except httpx.TimeoutException as exc:
            # 转成内置 TimeoutError，让 invoke() 映射为 UPSTREAM_TIMEOUT 并重试
            raise TimeoutError(f"抓取 {current} 超时") from exc
        except httpx.HTTPError as exc:
            return None, f"请求失败: {type(exc).__name__}: {exc}"

    return None, f"重定向次数超过 {MAX_REDIRECTS} 次"


# ---------------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """极简正文抽取器：丢标签、留文本、保块级换行。

    跳过状态用栈：``<script>`` 未闭合时不会像计数器那样永久卡住。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._chunks: list[str] = []
        self.title = ""
        self._in_title = False
        # 结束时若跳过栈非空，说明 HTML 结构异常，正文可能不完整
        self.unbalanced_skip = False

    @property
    def _skipping(self) -> bool:
        return bool(self._skip_stack)

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in SKIP_TAGS:
            self._skip_stack.append(tag)
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            # 弹到匹配的标签为止，兼容错位嵌套
            if tag in self._skip_stack:
                while self._skip_stack and self._skip_stack.pop() != tag:
                    pass
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skipping:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        else:
            self._chunks.append(text + " ")

    def close(self) -> None:
        super().close()
        self.unbalanced_skip = bool(self._skip_stack)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


@register(
    name="fetch_webpage",
    description=(
        "抓取指定网页的标题与正文文本。用于获取旅行攻略、签证政策、景点开放时间等"
        "实时信息。返回内容已截断，请勿假设能拿到全文。"
        "出于安全考虑，内网地址（含 localhost、私有网段、云元数据端点）会被拒绝。"
    ),
    params_model=WebParams,
    idempotent=True,
    tags=["外部API", "网页"],
)
def fetch_webpage(params: WebParams) -> ToolResult:
    netloc = urlparse(params.url).netloc

    html, error = _fetch_limited(params.url, timeout=tool_timeout())
    if html is None:
        code = ERR_BAD_ARGS if "拒绝访问" in error else ERR_UPSTREAM_ERROR
        return ToolResult.failure(code, error, source=netloc)

    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.get_text()

    # 动态渲染页面（SPA）或结构异常时可能一个字都抽不到。
    # 这种情况必须报错而不是返回「空的成功结果」—— 否则模型会以为页面真的没内容。
    if not text:
        return ToolResult.failure(
            ERR_UPSTREAM_ERROR,
            f"未能从页面提取到正文（原始 HTML {len(html)} 字符）。"
            "该页面可能是动态渲染（需要 JS 执行）或结构异常。"
            "请改用其他来源，不要基于本页内容做判断。",
            source=netloc,
        )

    truncated = len(text) > params.max_chars
    data: dict[str, object] = {
        "url": params.url,
        "title": parser.title.strip() or "(无标题)",
        "text": text[: params.max_chars],
        "truncated": truncated,
        "total_chars": len(text),
    }
    if parser.unbalanced_skip:
        data["parse_warning"] = (
            "页面存在未闭合的 script/style 标签，正文可能不完整，请谨慎采信。"
        )
    return ToolResult.success(data, source=netloc)
