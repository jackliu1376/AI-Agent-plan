"""工具 T2：fetch_webpage —— 抓取网页正文（攻略、签证政策、开放时间）。

只允许 http/https，且做长度截断，避免把整站塞进上下文烧 token。
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_BAD_ARGS, ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.tools.base import env_int, register

USER_AGENT = "TaskPlannerBot/0.1 (+educational use; respects robots.txt)"

# 不参与正文的标签
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe"}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
}


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


class _TextExtractor(HTMLParser):
    """极简正文抽取器：丢标签、留文本、保块级换行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:  # noqa: ANN001
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
        else:
            self._chunks.append(text + " ")

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


@register(
    name="fetch_webpage",
    description=(
        "抓取指定网页的标题与正文文本。用于获取旅行攻略、签证政策、景点开放时间等"
        "实时信息。返回内容已截断，请勿假设能拿到全文。"
    ),
    params_model=WebParams,
    idempotent=True,
    tags=["外部API", "网页"],
)
def fetch_webpage(params: WebParams) -> ToolResult:
    timeout = env_int("TOOL_TIMEOUT", 10)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(params.url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return ToolResult.failure(
            ERR_UPSTREAM_ERROR, f"目标站点返回 {exc.response.status_code}"
        )

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return ToolResult.failure(
            ERR_BAD_ARGS, f"不支持的内容类型: {content_type or 'unknown'}"
        )

    parser = _TextExtractor()
    parser.feed(resp.text)
    text = parser.get_text()

    truncated = len(text) > params.max_chars
    return ToolResult.success(
        {
            "url": params.url,
            "title": parser.title.strip() or "(无标题)",
            "text": text[: params.max_chars],
            "truncated": truncated,
            "total_chars": len(text),
        },
        source=urlparse(params.url).netloc,
    )
