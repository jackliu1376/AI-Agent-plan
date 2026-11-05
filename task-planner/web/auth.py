"""访问令牌鉴权。

为什么需要它
------------
``web/app.py`` 的接口默认没有任何鉴权。跑在 127.0.0.1 上时这是合理的
（能访问就说明已经在这台机器上），但一旦部署到公网，等于把三样东西
一起开放：

- ``POST /api/sessions`` —— 任何人提交任务都在烧你的模型余额
- ``GET /api/sessions``  —— 列出全部历史任务原文
- ``DELETE /api/sessions/{id}`` —— 删掉你的会话

限流（默认 10 次/窗口）防的是「刷」，不是「用」。10 次真实任务足够产生账单。

设计取舍
--------
**为什么凭证走 Cookie 而不是 Authorization 头**

前端的 SSE 用浏览器原生 ``EventSource`` 订阅，而 ``EventSource`` 无法设置
自定义请求头。只做 header 鉴权的话，事件流会被 401 挡在门外，页面卡在
「正在查证」永不结束。Cookie 由浏览器对同源请求自动携带，``EventSource``
也吃这一套，是这里唯一可行的方案。

同时保留 ``Authorization: Bearer`` —— 用 curl 调试时比手动拼 Cookie 方便。

**Cookie 里不放原始令牌**

存的是 ``HMAC-SHA256(token, "cairn-auth-cookie-v1")``。即使 cookie 被日志、
截图、代理访问记录带走，也拿不到能直接配置到别处的原始令牌。代价是无法
单独吊销某个 cookie —— 换 ``WEB_ACCESS_TOKEN`` 会让所有已登录会话失效，
这个场景下可以接受。

**留空即关闭**

``WEB_ACCESS_TOKEN`` 未配置时整个中间件直接放行。本地开发不需要任何
额外操作，行为与加鉴权之前完全一致。
"""

from __future__ import annotations

import hashlib
import hmac
from http.cookies import SimpleCookie
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE_NAME = "cairn_auth"
COOKIE_MAX_AGE = 7 * 24 * 3600
_HMAC_MESSAGE = b"cairn-auth-cookie-v1"

# 不需要鉴权的路径。判断放在中间件最前面，顺序即优先级。
#
# - ``/api/health``：探活用。运维脚本、负载均衡、前端健康检查都要能访问，
#   否则服务挂了和鉴权失败分不清。它会暴露模型名，可以接受。
# - ``/api/auth/*``：登录本身当然不能要求已登录。
PUBLIC_PATHS = frozenset(
    {
        "/api/health",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/logout",
    }
)


def cookie_value(token: str) -> str:
    """由访问令牌推导出 cookie 值。同一个 token 恒定得到同一个值。"""
    return hmac.new(token.encode("utf-8"), _HMAC_MESSAGE, hashlib.sha256).hexdigest()


def _bearer(headers: dict[bytes, bytes]) -> str:
    raw = headers.get(b"authorization", b"").decode("latin-1")
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _cookie(headers: dict[bytes, bytes]) -> str:
    raw = headers.get(b"cookie", b"").decode("latin-1")
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        # 畸形 cookie 按「没带凭证」处理，而不是抛出去变成 500 ——
        # 一个乱码的 cookie 不该让整个请求失败。
        return ""
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else ""


def is_authorized(headers: dict[bytes, bytes], token: str) -> bool:
    """请求是否携带了有效凭证。用 ``compare_digest`` 避免计时侧信道。"""
    if not token:
        return True

    expected = cookie_value(token)
    candidates = [_cookie(headers), _bearer(headers)]
    # Bearer 里放的是原始令牌，Cookie 里是 HMAC —— 两种都算有效
    for candidate in candidates:
        if not candidate:
            continue
        if hmac.compare_digest(candidate, expected):
            return True
        if hmac.compare_digest(candidate, token):
            return True
    return False


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    # 非 /api 开头的都是前端静态资源。登录页本身要能加载，
    # 否则用户连输入令牌的界面都看不到。
    return not path.startswith("/api/")


class AccessTokenMiddleware:
    """纯 ASGI 中间件。

    **刻意不用 BaseHTTPMiddleware**：后者会包装 response body 迭代器，
    对 SSE 这种长连接容易引入额外缓冲，表现是事件被攒着一起发。这里只读
    ``scope["headers"]``，完全不碰 body，对事件流零影响。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token: str = scope["app"].state.access_token
        if not token:
            await self.app(scope, receive, send)
            return

        # CORS 预检不带凭证，必须放行，否则浏览器连真正的请求都发不出去
        if scope["method"] == "OPTIONS" or _is_public(scope["path"]):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope["headers"]}
        if is_authorized(headers, token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"detail": "需要访问令牌。请在页面上输入 WEB_ACCESS_TOKEN 后重试。"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


def cookie_kwargs(*, secure: bool) -> dict[str, Any]:
    """``set_cookie`` 的公共参数。

    ``httponly`` 让 JS 读不到（挡 XSS 偷凭证）；``samesite=lax`` 挡掉
    跨站表单提交（本项目是同源部署，不影响正常使用）。
    ``secure`` 只在 HTTPS 下开 —— 本地 http://localhost 设了会导致
    浏览器直接丢弃这个 cookie，表现为「登录成功但刷新又回到登录页」。
    """
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": COOKIE_MAX_AGE,
    }
