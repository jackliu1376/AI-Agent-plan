"""访问令牌鉴权测试。

重点不是「密码对不对」，而是三个容易在部署后才暴露的问题：

1. **未配置令牌时必须完全放行** —— 本地开发不能因为加了这个功能就多一道门槛
2. **Cookie 在 HTTP 下不能带 Secure** —— 带了浏览器会静默丢弃，
   表现为「提示登录成功、刷新又回登录页」，且控制台没有任何报错
3. **SSE 必须能被鉴权** —— 前端用 EventSource 订阅，它不能带自定义头，
   所以凭证只能走 Cookie。这条路径断了，页面会卡在「正在查证」永不结束
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent.config import Settings
from agent.demo import ScriptedLLM
from tests.conftest import make_plan, plan_turn
from web.app import create_app
from web.auth import COOKIE_NAME, cookie_value
from web.session import SessionStore

TOKEN = "test-token-9f3a"


def _app(settings: Settings, *, token: str = TOKEN, rate_limit: int = 100):
    def factory(_settings: Settings):
        return ScriptedLLM([plan_turn(make_plan())])

    return create_app(
        settings=settings,
        llm_factory=factory,
        store=SessionStore(),
        rate_limit=rate_limit,
        access_token=token,
    )


@pytest.fixture
async def make_client():
    clients: list[AsyncClient] = []

    def _make(app) -> AsyncClient:
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


# ---------------------------------------------------------------------------
# 未配置令牌：行为必须和加鉴权之前完全一致
# ---------------------------------------------------------------------------


async def test_without_token_everything_is_open(settings: Settings, make_client) -> None:
    client = make_client(_app(settings, token=""))

    status = await client.get("/api/auth/status")
    assert status.json() == {"required": False, "authenticated": True}

    # 业务接口直接可用，不需要任何凭证
    created = await client.post("/api/sessions", json={"task": "周末去成都玩两天"})
    assert created.status_code == 200
    assert (await client.get("/api/sessions")).status_code == 200


# ---------------------------------------------------------------------------
# 配了令牌：受保护接口必须拒绝
# ---------------------------------------------------------------------------


async def test_status_reports_required_when_token_configured(
    settings: Settings, make_client
) -> None:
    client = make_client(_app(settings))
    body = (await client.get("/api/auth/status")).json()
    assert body == {"required": True, "authenticated": False}


async def test_protected_endpoints_return_401(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))

    for method, path in [
        ("get", "/api/sessions"),
        ("get", "/api/tools"),
        ("get", "/api/map/config"),
        ("post", "/api/sessions"),
    ]:
        kwargs = {"json": {"task": "周末去成都玩两天"}} if method == "post" else {}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path} 应该被拦住"


async def test_sse_endpoint_requires_auth(settings: Settings, make_client) -> None:
    """事件流同样要鉴权 —— 否则别人能实时看到你的任务内容。"""
    client = make_client(_app(settings))
    resp = await client.get("/api/sessions/whatever/events")
    assert resp.status_code == 401


async def test_health_stays_public(settings: Settings, make_client) -> None:
    """健康检查必须公开：否则服务挂了和鉴权失败分不清。"""
    client = make_client(_app(settings))
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_static_files_stay_public(settings: Settings, make_client) -> None:
    """登录页本身要能加载 —— 静态资源不能被鉴权挡住。"""
    client = make_client(_app(settings))
    resp = await client.get("/")
    # 没有构建产物时是 404，但绝不能是 401
    assert resp.status_code != 401


# ---------------------------------------------------------------------------
# 两种凭证形式
# ---------------------------------------------------------------------------


async def test_bearer_token_accepted(settings: Settings, make_client) -> None:
    """方便 curl 调试：Authorization: Bearer <token> 直接可用。"""
    client = make_client(_app(settings))
    resp = await client.get("/api/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200


async def test_wrong_token_rejected(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))
    resp = await client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


async def test_login_sets_cookie_and_grants_access(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))

    resp = await client.post("/api/auth/login", json={"token": TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "required": True}

    # httpx 会自动保存 Set-Cookie，后续请求带上它
    assert (await client.get("/api/sessions")).status_code == 200
    assert (await client.get("/api/auth/status")).json()["authenticated"] is True


async def test_login_with_wrong_token_rejected(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))
    resp = await client.post("/api/auth/login", json={"token": "nope"})
    assert resp.status_code == 401
    assert (await client.get("/api/sessions")).status_code == 401


async def test_logout_clears_cookie(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))
    await client.post("/api/auth/login", json={"token": TOKEN})
    assert (await client.get("/api/sessions")).status_code == 200

    await client.post("/api/auth/logout")
    assert (await client.get("/api/sessions")).status_code == 401


# ---------------------------------------------------------------------------
# Cookie 属性：本地 HTTP 与线上 HTTPS 必须区分
# ---------------------------------------------------------------------------


async def test_cookie_has_no_secure_flag_over_http(settings: Settings, make_client) -> None:
    """HTTP 下不能带 Secure —— 带了浏览器会直接丢弃，登录等于没成功。"""
    client = make_client(_app(settings))
    resp = await client.post("/api/auth/login", json={"token": TOKEN})
    raw = resp.headers["set-cookie"].lower()
    assert "secure" not in raw
    assert "httponly" in raw
    assert "samesite=lax" in raw


async def test_cookie_has_secure_flag_behind_https_proxy(
    settings: Settings, make_client
) -> None:
    """Nginx 终止 TLS 后，后端看到的是 http，靠 X-Forwarded-Proto 判断。"""
    client = make_client(_app(settings))
    resp = await client.post(
        "/api/auth/login",
        json={"token": TOKEN},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert "secure" in resp.headers["set-cookie"].lower()


async def test_cookie_value_is_not_the_raw_token(settings: Settings, make_client) -> None:
    """Cookie 里放 HMAC 而不是原始令牌。

    这样即使 cookie 被日志、截图、代理访问记录带走，也拿不到能配置到
    别处的原始令牌。
    """
    client = make_client(_app(settings))
    resp = await client.post("/api/auth/login", json={"token": TOKEN})
    assert cookie_value(TOKEN) != TOKEN
    assert cookie_value(TOKEN) in resp.headers["set-cookie"]
    assert TOKEN not in resp.headers["set-cookie"]


async def test_forged_cookie_rejected(settings: Settings, make_client) -> None:
    client = make_client(_app(settings))
    client.cookies.set(COOKIE_NAME, "deadbeef")
    assert (await client.get("/api/sessions")).status_code == 401


# ---------------------------------------------------------------------------
# 登录限流
# ---------------------------------------------------------------------------


async def test_login_is_rate_limited(settings: Settings, make_client) -> None:
    """登录接口单独限流 5 次/分钟，防暴力破解。

    全站限流是 10 次/窗口，拿来挡爆破太宽松了。
    """
    client = make_client(_app(settings))
    codes = []
    for _ in range(7):
        resp = await client.post("/api/auth/login", json={"token": "wrong"})
        codes.append(resp.status_code)

    assert codes[:5] == [401] * 5
    assert codes[5] == 429
