"""Web 层测试（FastAPI + SSE）。

用 ``httpx.ASGITransport`` 直接驱动 ASGI 应用，不启真实端口。
``llm_factory`` 注入脚本化 LLM，因此整条 HTTP + SSE 链路都能离线测试。

覆盖三个最容易出错的地方：

1. **事件重放** —— 客户端晚连 / 断线重连都不能丢事件
2. **澄清跨请求** —— 提问走 SSE 推送，回答走另一个 HTTP 请求回来
3. **不阻塞事件循环** —— 同步的 ``llm.chat()`` 必须放进线程池，
   否则一个请求会卡住整个服务
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent.config import Settings
from agent.demo import ScriptedLLM
from tests.conftest import make_plan, plan_turn, tool_turn
from web.app import create_app
from web.session import SessionStore

WEATHER_ARGS = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}


def normal_script() -> list:
    return [tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)), plan_turn(make_plan())]


def clarify_script() -> list:
    return [
        tool_turn(("c1", "ask_user_clarification", {"question": "去哪？几号？"})),
        plan_turn(make_plan()),
    ]


class SlowLLM:
    """模拟真实 LLM 的同步阻塞（一次调用几百毫秒到几十秒）。"""

    def __init__(self, inner: Any, delay: float) -> None:
        self._inner = inner
        self._delay = delay

    def chat(self, messages, tools=None):  # noqa: ANN001
        time.sleep(self._delay)
        return self._inner.chat(messages, tools=tools)


@pytest.fixture
def make_app(settings: Settings):
    """返回一个工厂：给定脚本即可构建应用。"""

    def _build(script, *, rate_limit: int = 100, llm_wrapper=None):
        def factory(_settings: Settings):
            llm = ScriptedLLM(script())
            return llm_wrapper(llm) if llm_wrapper else llm

        return create_app(
            settings=settings,
            llm_factory=factory,
            store=SessionStore(),
            rate_limit=rate_limit,
        )

    return _build


@pytest.fixture
async def client_factory():
    clients: list[AsyncClient] = []

    def _make(app) -> AsyncClient:
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()


async def collect_sse(
    client: AsyncClient, url: str, *, stop_at: set[str], timeout: float = 20.0
) -> list[dict]:
    """读取 SSE 直到出现指定事件类型或超时。"""
    events: list[dict] = []

    async def _read() -> None:
        async with client.stream("GET", url) as resp:
            assert resp.status_code == 200, resp.status_code
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: ") :])
                events.append(event)
                if event["type"] in stop_at:
                    return

    await asyncio.wait_for(_read(), timeout=timeout)
    return events


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------


async def test_health(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model"]


async def test_list_tools(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    resp = await client.get("/api/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 10
    names = {t["name"] for t in body["tools"]}
    assert "get_weather_forecast" in names
    assert "query_attractions_db" in names
    # 前端要拿 parameters 渲染表单，必须带上
    assert all(t["parameters"]["type"] == "object" for t in body["tools"])


async def test_unknown_session_returns_404(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    assert (await client.get("/api/sessions/nope")).status_code == 404
    assert (await client.get("/api/sessions/nope/events")).status_code == 404
    assert (await client.delete("/api/sessions/nope")).status_code == 404


async def test_unknown_api_path_returns_404_not_405(make_app, client_factory) -> None:
    """回归：未知的 /api 路径用 POST 访问，不能落到静态文件挂载点上。

    ``StaticFiles`` 只服务 GET/HEAD，POST 会得到 405「Method Not Allowed」——
    那个报错会把排查方向带偏：看起来像客户端用错了 HTTP 方法，
    实际是后端没有这条路由（最常见的原因是改了代码但没重启服务）。

    这不是假想：改增量修订那次，前端已经调 `/revisions` 而后端进程还是旧的，
    界面上就只显示一行「Method Not Allowed」，看不出是版本没对齐。
    """
    client = client_factory(make_app(normal_script))

    for path in ("/api/does-not-exist", "/api/sessions/x/unknown-action"):
        resp = await client.post(path, json={})
        assert resp.status_code == 404, f"POST {path} 落到了静态挂载点上"
        assert "没有这个接口" in resp.json()["detail"]

    # GET 也要走同一条兜底 —— 否则响应体是一段 HTML，前端解析不出 detail
    assert (await client.get("/api/does-not-exist")).status_code == 404


async def test_static_files_still_served(make_app, client_factory) -> None:
    """兜底路由只吃 /api/*，不能把前端产物挡掉。"""
    client = client_factory(make_app(normal_script))
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# 完整流程
# ---------------------------------------------------------------------------


async def test_create_session_and_stream_events(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    created = await client.post("/api/sessions", json={"task": "成都 2 日游"})
    assert created.status_code == 200
    body = created.json()
    sid = body["session_id"]
    assert body["events_url"].endswith(f"/{sid}/events")

    events = await collect_sse(client, body["events_url"], stop_at={"plan_ready"})
    types = [e["type"] for e in events]

    assert types[0] == "run_started"
    assert types[-1] == "plan_ready"
    assert "tool_call" in types and "tool_result" in types

    # 事件顺序必须正确：tool_call 在 tool_result 之前
    assert types.index("tool_call") < types.index("tool_result")


async def test_session_view_exposes_result(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})

    view = (await client.get(f"/api/sessions/{sid}")).json()
    assert view["status"] == "done"
    assert view["error"] is None
    assert view["result"]["ok"] is True
    assert view["result"]["step_count"] == 2
    assert view["event_count"] >= 5


async def test_late_subscriber_gets_replay(make_app, client_factory) -> None:
    """客户端晚连（甚至任务已跑完）也要能拿到完整事件。"""
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]

    # 故意等到任务跑完再连
    for _ in range(100):
        view = (await client.get(f"/api/sessions/{sid}")).json()
        if view["status"] == "done":
            break
        await asyncio.sleep(0.02)

    events = await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})
    assert [e["type"] for e in events][0] == "run_started"
    assert events[-1]["type"] == "plan_ready"


async def test_cursor_resumes_without_duplicates(make_app, client_factory) -> None:
    """断线重连：带上已收到的条数，只拿增量。"""
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    first = await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})

    again = await collect_sse(
        client,
        f"/api/sessions/{sid}/events?cursor={len(first) - 1}",
        stop_at={"plan_ready"},
    )
    assert [e["type"] for e in again] == [first[-1]["type"]]


# ---------------------------------------------------------------------------
# 澄清：跨请求的挂起与唤醒
# ---------------------------------------------------------------------------


async def test_clarification_flow_over_http(make_app, client_factory) -> None:
    """澄清全链路：SSE 推问题 → 另一个请求提交答案 → 协程恢复 → 产出计划。

    注意用**两个客户端**：SSE 流会长时间占住一个连接，
    在同一客户端里嵌套发请求会和 ASGI transport 互相等待。
    """
    app = make_app(clarify_script)
    stream_client = client_factory(app)
    api_client = client_factory(app)

    sid = (
        await api_client.post("/api/sessions", json={"task": "我想去旅行"})
    ).json()["session_id"]

    async def answer_when_asked() -> bool:
        for _ in range(400):
            view = (await api_client.get(f"/api/sessions/{sid}")).json()
            if view["status"] == "awaiting_input":
                assert view["clarification"]["question"] == "去哪？几号？"
                resp = await api_client.post(
                    f"/api/sessions/{sid}/answers", json={"answer": "成都，10 月 3-4 日"}
                )
                assert resp.status_code == 200, resp.text
                return True
            if view["status"] in ("done", "failed"):
                return False
            await asyncio.sleep(0.02)
        return False

    answered, events = await asyncio.gather(
        answer_when_asked(),
        collect_sse(stream_client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"}),
    )

    assert answered is True, "服务端始终没有进入 awaiting_input 状态"
    types = [e["type"] for e in events]
    assert types.count("clarification") == 1
    assert events[-1]["type"] == "plan_ready"

    view = (await api_client.get(f"/api/sessions/{sid}")).json()
    assert view["status"] == "done"
    assert view["clarification"] is None
    assert view["result"]["ok"] is True


async def test_answer_without_pending_question_returns_409(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    resp = await client.post(f"/api/sessions/{sid}/answers", json={"answer": "成都"})
    assert resp.status_code == 409


async def test_answer_validation(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    # 空答案不合法
    resp = await client.post(f"/api/sessions/{sid}/answers", json={"answer": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 不阻塞事件循环（把同步调用放进线程池）
# ---------------------------------------------------------------------------


async def test_slow_llm_does_not_block_other_requests(make_app, client_factory) -> None:
    """回归：``llm.chat()`` 是同步的，若直接在协程里调用会冻住整个服务。

    这里让 LLM 每次阻塞 0.5 秒，然后测量 ``/api/health`` 的响应时间 ——
    如果事件循环被占住，health 会等到 LLM 返回才响应。
    """
    client = client_factory(make_app(normal_script, llm_wrapper=lambda llm: SlowLLM(llm, 0.5)))
    await client.post("/api/sessions", json={"task": "成都 2 日游"})
    await asyncio.sleep(0.1)  # 让后台任务真正进入 LLM 调用

    started = time.monotonic()
    resp = await client.get("/api/health")
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert elapsed < 0.2, f"health 被阻塞了 {elapsed:.2f}s，说明同步调用没放进线程池"


# ---------------------------------------------------------------------------
# 限流 / 生命周期 / 失败
# ---------------------------------------------------------------------------


async def test_rate_limit_returns_429(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script, rate_limit=2))
    assert (await client.post("/api/sessions", json={"task": "任务一"})).status_code == 200
    assert (await client.post("/api/sessions", json={"task": "任务二"})).status_code == 200

    blocked = await client.post("/api/sessions", json={"task": "任务三"})
    assert blocked.status_code == 429
    assert "限流" in blocked.json()["detail"]


async def test_task_validation(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    assert (await client.post("/api/sessions", json={"task": "x"})).status_code == 422
    assert (await client.post("/api/sessions", json={})).status_code == 422


async def test_delete_session(make_app, client_factory) -> None:
    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]

    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
    assert (await client.get(f"/api/sessions/{sid}")).status_code == 404


async def test_llm_failure_becomes_failed_status(make_app, client_factory) -> None:
    """LLM 抛异常必须变成会话状态，不能让后台任务静默死掉。"""

    class ExplodingLLM:
        def chat(self, messages, tools=None):  # noqa: ANN001
            raise RuntimeError("上游炸了")

    client = client_factory(make_app(normal_script, llm_wrapper=lambda _: ExplodingLLM()))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]

    for _ in range(150):
        view = (await client.get(f"/api/sessions/{sid}")).json()
        if view["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)

    assert view["status"] == "failed"
    assert "上游炸了" in view["error"]

    events = await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"error"})
    assert events[-1]["type"] == "error"
    assert "上游炸了" in events[-1]["data"]["message"]


async def test_invalid_output_marks_done_but_not_ok(make_app, client_factory) -> None:
    """模型没产出合规计划 ≠ 运行失败：会话是 done，但 result.ok 为 False。"""
    from tests.conftest import text_turn

    client = client_factory(
        make_app(lambda: [text_turn("没有 JSON"), text_turn("还是没有 JSON")])
    )
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})

    view = (await client.get(f"/api/sessions/{sid}")).json()
    assert view["status"] == "done"
    assert view["result"]["ok"] is False
    assert view["result"]["plan"] is None
    assert view["result"]["trace"]["output_status"] == "invalid_output"


# ---------------------------------------------------------------------------
# 会话回收必须同时取消后台任务（否则协程永久挂在 Future 上）
# ---------------------------------------------------------------------------


def test_store_evict_invokes_hook() -> None:
    """TTL 回收必须触发 on_evict，否则卡在澄清的协程永不退出。"""
    from web.session import SessionStore

    evicted: list[str] = []
    store = SessionStore(ttl_seconds=0.0, on_evict=evicted.append)

    session = store.create("任务一")
    # ttl=0 时下一次 create 会把它回收掉
    store.create("任务二")

    assert session.id in evicted, "超时会话被回收时没有触发 on_evict"
    assert store.get(session.id) is None


def test_store_drop_invokes_hook() -> None:
    from web.session import SessionStore

    evicted: list[str] = []
    store = SessionStore(on_evict=evicted.append)
    session = store.create("任务")

    assert store.drop(session.id) is True
    assert evicted == [session.id]
    assert store.drop(session.id) is False, "重复 drop 应返回 False"


def test_store_evict_hook_failure_does_not_break_cleanup() -> None:
    """钩子抛异常不该影响回收本身。"""
    from web.session import SessionStore

    def boom(_: str) -> None:
        raise RuntimeError("钩子炸了")

    store = SessionStore(ttl_seconds=0.0, on_evict=boom)
    session = store.create("任务一")
    store.create("任务二")  # 触发回收

    assert store.get(session.id) is None


async def test_evicted_session_cancels_background_task(make_app, client_factory) -> None:
    """核心回归：TTL 回收会话后，它的后台任务必须被取消。"""
    app = make_app(clarify_script)
    client = client_factory(app)

    sid = (await client.post("/api/sessions", json={"task": "我想去旅行"})).json()["session_id"]

    # 等到进入澄清（协程挂起在 Future 上）
    for _ in range(300):
        view = (await client.get(f"/api/sessions/{sid}")).json()
        if view["status"] == "awaiting_input":
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("未进入 awaiting_input")

    task = app.state.tasks.get(sid)
    assert task is not None and not task.done()

    # 模拟 TTL 到期
    app.state.store.drop(sid)
    await asyncio.sleep(0.1)

    assert task.cancelled() or task.done(), "会话被回收后，后台任务仍挂着"
    assert sid not in app.state.tasks


async def test_delete_session_cancels_task(make_app, client_factory) -> None:
    app = make_app(clarify_script)
    client = client_factory(app)
    sid = (await client.post("/api/sessions", json={"task": "我想去旅行"})).json()["session_id"]

    for _ in range(300):
        view = (await client.get(f"/api/sessions/{sid}")).json()
        if view["status"] == "awaiting_input":
            break
        await asyncio.sleep(0.02)

    task = app.state.tasks.get(sid)
    assert task is not None

    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
    await asyncio.sleep(0.1)
    assert task.cancelled() or task.done()
    assert sid not in app.state.tasks


# ---------------------------------------------------------------------------
# 限流取客户端标识（反向代理场景）
# ---------------------------------------------------------------------------


def test_client_key_uses_forwarded_header_only_when_trusted() -> None:
    from web.app import _client_key

    class _Req:
        def __init__(self) -> None:
            self.headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
            self.client = type("C", (), {"host": "10.0.0.1"})()

    request = _Req()
    assert _client_key(request, trust_proxy=False) == "10.0.0.1", "不信任代理时不能采信 XFF"
    assert _client_key(request, trust_proxy=True) == "203.0.113.9", "信任代理时应取第一跳"


def test_client_key_falls_back_when_no_client() -> None:
    from web.app import _client_key

    class _Req:
        headers: dict[str, str] = {}
        client = None

    assert _client_key(_Req(), trust_proxy=False) == "unknown"


# ---------------------------------------------------------------------------
# 运行记录：Web 层也要落盘
# ---------------------------------------------------------------------------


async def test_web_run_writes_run_log(
    make_app,
    client_factory,
    monkeypatch: pytest.MonkeyPatch,
    run_log_path,
) -> None:
    """Web 层跑完必须写运行记录，且 source 标记为 web。"""
    from agent.run_log import load_runs

    monkeypatch.setenv("RUN_LOG_ENABLED", "1")
    monkeypatch.setenv("RUN_LOG_PATH", str(run_log_path))

    client = client_factory(make_app(normal_script))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]
    await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})

    records = load_runs(run_log_path)
    assert len(records) == 1, "Web 层没有写入运行记录"
    record = records[0]
    assert record["source"] == "web"
    assert record["task"] == "成都 2 日游"
    assert record["ok"] is True
    assert record["step_count"] > 0


async def test_web_failed_run_is_also_logged(
    make_app,
    client_factory,
    monkeypatch: pytest.MonkeyPatch,
    run_log_path,
) -> None:
    """失败样本才是排查重点，必须一并记录。"""
    from agent.run_log import load_runs

    monkeypatch.setenv("RUN_LOG_ENABLED", "1")
    monkeypatch.setenv("RUN_LOG_PATH", str(run_log_path))

    class ExplodingLLM:
        def chat(self, messages, tools=None):  # noqa: ANN001
            raise RuntimeError("上游炸了")

    client = client_factory(make_app(normal_script, llm_wrapper=lambda _: ExplodingLLM()))
    sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()["session_id"]

    for _ in range(200):
        view = (await client.get(f"/api/sessions/{sid}")).json()
        if view["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.02)

    records = load_runs(run_log_path)
    assert len(records) == 1
    assert records[0]["ok"] is False
    assert records[0]["output_status"] == "runtime_error"
    assert "上游炸了" in records[0]["error"]
