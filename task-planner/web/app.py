"""Web 层：把编排循环包装成 HTTP + SSE 服务。

分层
----
::

    web/app.py      路由、SSE、限流（本文件）
    web/session.py  会话状态、事件重放、澄清挂起/唤醒

与 CLI 的关系
-------------
两者共用同一个 ``Orchestrator``。差别只在于：

- CLI 把 ``ask_user`` 接到 ``input()``；Web 接到一个 ``asyncio.Future``
- CLI 直接 ``print`` 事件；Web 把事件写进会话并推给 SSE 订阅者

因此 Web 层没有重复任何编排逻辑。

安全
----
- ``DEEPSEEK_API_KEY`` 只存在于服务端，绝不下发到前端。
- 按 IP 限流，防止被刷爆额度。
- 会话有 TTL 与数量上限，防止内存无限增长。

持久化
------
会话在状态跃迁时落盘到 ``logs/sessions.db``（见 ``web/session_db.py``），
因此「最近」列表里的记录在服务重启后依然打得开。
``SESSION_DB_ENABLED=0`` 可关掉，退回纯内存。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.config import Settings
from agent.events import Event
from agent.loop import Orchestrator, revision_instruction, wrap_user_input
from agent.run_log import record_run, settings_params
from agent.tool_runner import LocalToolRunner
from mcp_server.tools.base import load_all_tools, openai_tool_schemas
from web.session import LIVE_STATUSES, Session, SessionStore
from web.session_db import SessionDB, is_enabled

LLMFactory = Callable[[Settings], Any]

# 前端构建产物目录（Step 3 产出）。存在时由本服务直接托管，免去单独部署静态站点。
STATIC_DIR = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    task: str = Field(min_length=2, max_length=500, description="自然语言任务描述")


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str
    events_url: str
    answer_url: str


class AnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=1000)


class RevisionRequest(BaseModel):
    """一次增量修订。``feedback`` 是用户的原话（「第二天太赶了」）。"""

    feedback: str = Field(min_length=1, max_length=2000, description="修改意见")


class RevisionResponse(BaseModel):
    ok: bool
    session_id: str
    seq: int
    # 新一次运行的事件从这个下标开始。事件是**追加**的，
    # 不复用这个游标的话客户端会把上一版的进度重放一遍。
    cursor: int
    events_url: str


class RevisionSummary(BaseModel):
    """版本菜单用的一行。不含计划正文。"""

    seq: int
    feedback: str
    created_at: float
    ok: bool
    step_count: int
    revision_summary: str = ""


class RevisionView(BaseModel):
    """某一版的完整内容。"""

    session_id: str
    seq: int
    feedback: str
    created_at: float
    result: dict[str, Any]


class SessionView(BaseModel):
    session_id: str
    task: str
    status: str
    clarification: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    event_count: int
    # 事件一并返回：从磁盘恢复一条旧记录时，前端要用它渲染「查证了 N 项」。
    # 只给结果不给过程的话，恢复出来的页面会比刚跑完时少一块内容。
    events: list[dict[str, Any]] = Field(default_factory=list)
    # 版本摘要（不含计划正文）。正文走 GET /sessions/{id}/revisions/{seq}。
    revisions: list[RevisionSummary] = Field(default_factory=list)


class SessionSummary(BaseModel):
    """「最近」列表的一行。刻意不含 events / result —— 列表不需要正文。"""

    session_id: str
    task: str
    status: str
    created_at: float
    updated_at: float
    ok: bool | None = None


class SessionListResponse(BaseModel):
    count: int
    sessions: list[SessionSummary]


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------


class RateLimiter:
    """按 key 的滑动窗口限流。单机内存实现。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._hits: dict[str, list[float]] = {}
        self._limit = limit
        self._window = window_seconds

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._hits.get(key, []) if now - t < self._window]
        if len(recent) >= self._limit:
            self._hits[key] = recent
            return False
        recent.append(now)
        self._hits[key] = recent
        return True

    def retry_after(self, key: str) -> int:
        hits = self._hits.get(key) or []
        if not hits:
            return 0
        return max(1, int(self._window - (time.monotonic() - hits[0])))


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def _default_llm_factory(settings: Settings) -> Any:
    from agent.llm_client import DeepSeekClient

    return DeepSeekClient(settings)


def _default_db() -> SessionDB | None:
    """按环境变量决定是否落盘。``SESSION_DB_ENABLED=0`` 时返回 None。

    返回 None 而不是一个「假的空库」，是为了让调用方一眼看出
    「这条路径上根本没有持久化」—— 比一个默默什么都不做的对象好排查。
    """
    return SessionDB() if is_enabled() else None


def create_app(
    *,
    settings: Settings | None = None,
    llm_factory: LLMFactory | None = None,
    store: SessionStore | None = None,
    db: SessionDB | None = None,
    rate_limit: int = 10,
    rate_window: float = 3600.0,
    janitor_interval: float = 60.0,
    trust_proxy: bool = False,
) -> FastAPI:
    """构建 FastAPI 应用。

    参数全部可注入，方便测试：测试里传 ``llm_factory`` 返回脚本化 LLM，
    就能在不消耗 API 额度的前提下跑完整 HTTP + SSE 链路；
    传 ``db=SessionDB(tmp_path / "s.db")`` 就能验证落盘与恢复。

    ``store`` 和 ``db`` 二选一即可：给了 ``store`` 就完全按它来，
    没给则按 ``db``（或环境变量）自建一个。
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 上次进程留下的「运行中」会话在这里被标成中断 —— 必须早于
        # 任何一次读列表，否则「最近」里会出现点不开的僵尸记录。
        _app.state.store.mark_interrupted()
        janitor = asyncio.create_task(_janitor_loop(_app, janitor_interval))
        try:
            yield
        finally:
            janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor

    app = FastAPI(
        title="Cairn API · 任务规划助手",
        version="0.2.0",
        description="把自然语言任务拆解为可执行计划。事件流走 SSE。",
        lifespan=lifespan,
    )

    app.state.settings = settings or Settings.load()
    app.state.llm_factory = llm_factory or _default_llm_factory
    app.state.store = store or SessionStore(
        db=db if db is not None else _default_db()
    )
    app.state.limiter = RateLimiter(rate_limit, rate_window)
    app.state.tasks = {}  # session_id -> asyncio.Task，防止被 GC
    app.state.trust_proxy = trust_proxy

    # 会话被回收时必须同时取消它的后台任务，否则协程会永久挂在
    # asyncio.Future 上（TTL 到期只是把会话从字典里删掉而已）。
    def _cancel_session_task(session_id: str) -> None:
        task = app.state.tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    app.state.store.set_on_evict(_cancel_session_task)

    # 开发期方便：Vite dev server 默认 5173
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    load_all_tools()

    # -- 基础 -------------------------------------------------------------

    @app.get("/api/health", summary="健康检查")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "model": app.state.settings.model,
            "active_sessions": app.state.store.size,
        }

    @app.get("/api/tools", summary="可用工具清单")
    async def list_tools() -> dict[str, Any]:
        schemas = openai_tool_schemas()
        return {
            "count": len(schemas),
            "tools": [
                {
                    "name": s["function"]["name"],
                    "description": s["function"]["description"],
                    "parameters": s["function"]["parameters"],
                }
                for s in schemas
            ],
        }

    # -- 会话 -------------------------------------------------------------

    @app.post("/api/sessions", response_model=CreateSessionResponse, summary="提交任务")
    async def create_session(payload: CreateSessionRequest, request: Request) -> Any:
        client = _client_key(request, trust_proxy=app.state.trust_proxy)
        if not app.state.limiter.allow(client):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"请求过于频繁。本服务按 IP 限流（{rate_limit} 次 / "
                    f"{int(rate_window // 60)} 分钟），请 {app.state.limiter.retry_after(client)} 秒后再试。"
                ),
            )

        session = app.state.store.create(payload.task)
        # 后台跑，事件写进会话；客户端晚一点连 SSE 也不会丢开头
        task = asyncio.create_task(_drive_session(app, session))
        app.state.tasks[session.id] = task
        task.add_done_callback(lambda _: app.state.tasks.pop(session.id, None))

        return CreateSessionResponse(
            session_id=session.id,
            status=session.status,
            events_url=f"/api/sessions/{session.id}/events",
            answer_url=f"/api/sessions/{session.id}/answers",
        )

    @app.get(
        "/api/sessions",
        response_model=SessionListResponse,
        summary="最近会话",
    )
    async def list_sessions(
        limit: int = Query(20, ge=1, le=100, description="最多返回多少条"),
    ) -> Any:
        """侧栏「最近」列表的数据源。

        为什么放在服务端而不是浏览器 localStorage：

        - 桌面版与浏览器打开的是同一个后端，历史该是同一份；
        - 本地缓存的列表指向的会话可能已经不在了（重启 / 换机器），
          服务端列表则天然是「真实存在的记录」。
        """
        sessions = app.state.store.list(limit=limit)
        return SessionListResponse(count=len(sessions), sessions=sessions)

    @app.get("/api/sessions/{session_id}", response_model=SessionView, summary="查询会话")
    async def get_session(session_id: str) -> Any:
        session = _require(app, session_id)
        return SessionView(
            session_id=session.id,
            task=session.task,
            status=session.status,
            clarification=session.clarification,
            result=session.result,
            error=session.error,
            event_count=len(session.events),
            events=session.events,
            revisions=session.revision_summaries(),
        )

    @app.get(
        "/api/sessions/{session_id}/revisions/{seq}",
        response_model=RevisionView,
        summary="取某一版计划的完整内容",
    )
    async def get_revision(session_id: str, seq: int) -> Any:
        """版本菜单里切到某一版时用它拿正文。

        单独开一个接口而不是塞进 ``SessionView``：正文动辄几十 KB，
        而 ``GET /sessions/{id}`` 是要被轮询的。
        """
        session = _require(app, session_id)
        result = session.revision_result(seq)
        if result is None:
            raise HTTPException(status_code=404, detail=f"没有第 {seq} 版。")

        record = next((r for r in session.revisions if r["seq"] == seq), {})
        return RevisionView(
            session_id=session.id,
            seq=seq,
            feedback=record.get("feedback", ""),
            created_at=record.get("created_at", 0.0),
            result=result,
        )

    @app.post(
        "/api/sessions/{session_id}/revisions",
        response_model=RevisionResponse,
        summary="按用户反馈增量修订计划",
    )
    async def create_revision(
        session_id: str, payload: RevisionRequest, request: Request
    ) -> Any:
        """在上一版计划的基础上改，而不是重新规划。

        三条前置检查都不是形式主义：

        - **限流**：每次修订都是一次完整的 LLM 调用，和新建会话等价。
        - **不能正在跑**：同一会话同时跑两次修订会互相覆盖事件与版本，
          而且 ``Orchestrator._last_result`` 是实例级状态，本来就不支持并发。
        - **必须有可改的计划**：没产出过计划的会话无从「增量」。
        """
        client = _client_key(request, trust_proxy=app.state.trust_proxy)
        if not app.state.limiter.allow(client):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"请求过于频繁。本服务按 IP 限流（{rate_limit} 次 / "
                    f"{int(rate_window // 60)} 分钟），请 {app.state.limiter.retry_after(client)} 秒后再试。"
                ),
            )

        session = _require(app, session_id)
        if session.status in LIVE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"这次规划还在运行（{session.status}），等它跑完再提修改。",
            )
        if session.result is None:
            raise HTTPException(
                status_code=409, detail="这条记录还没有可修改的计划。"
            )

        cursor = len(session.events)
        task = asyncio.create_task(_drive_revision(app, session, payload.feedback))
        app.state.tasks[session.id] = task
        task.add_done_callback(lambda _: app.state.tasks.pop(session.id, None))

        return RevisionResponse(
            ok=True,
            session_id=session.id,
            seq=len(session.revisions) + 1,
            cursor=cursor,
            events_url=f"/api/sessions/{session.id}/events",
        )

    @app.get("/api/sessions/{session_id}/events", summary="订阅事件流（SSE）")
    async def stream_events(
        session_id: str,
        cursor: int = Query(0, ge=0, description="已收到的事件条数，重连时传回可续传"),
    ) -> StreamingResponse:
        session = _require(app, session_id)

        async def event_source() -> AsyncIterator[str]:
            # 客户端断开时 Starlette 会取消这个生成器，无需额外清理
            async for item in session.stream(cursor):
                if item is None:
                    yield ": ping\n\n"  # SSE 注释行，仅用于保活
                else:
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # 关掉 Nginx 等反向代理的缓冲，否则事件会被攒着一起发
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/sessions/{session_id}/answers", summary="提交澄清答案")
    async def submit_answer(session_id: str, payload: AnswerRequest) -> dict[str, Any]:
        session = _require(app, session_id)
        if session.status != "awaiting_input":
            raise HTTPException(
                status_code=409,
                detail=f"当前会话状态为 {session.status}，没有待回答的问题。",
            )
        if not session.submit_answer(payload.answer.strip()):
            raise HTTPException(status_code=409, detail="答案未被接受，请重试。")
        return {"ok": True, "session_id": session.id}

    @app.delete("/api/sessions/{session_id}", summary="删除会话（内存 + 磁盘）")
    async def delete_session(session_id: str) -> dict[str, Any]:
        # drop() 会触发 on_evict 钩子，因此这里不需要单独 cancel。
        # 注意这是**硬删除**：磁盘上那一行也会没。回收（TTL）则只清内存。
        dropped = app.state.store.drop(session_id)
        if not dropped:
            raise HTTPException(status_code=404, detail="会话不存在或已过期。")
        return {"ok": True, "session_id": session_id}

    # -- 未知 /api 路径兜底 -------------------------------------------------
    #
    # 必须放在所有真实路由**之后**、静态挂载**之前**（FastAPI 按注册顺序匹配）。
    #
    # 没有它的话，POST 一个未注册的 /api 路径会落到下面的 StaticFiles 上，
    # 而它只服务 GET/HEAD —— 于是返回 405「Method Not Allowed」。
    # 这个报错会把排查方向带偏：看起来像客户端用错了 HTTP 方法，
    # 实际是后端根本没有这条路由（最常见的原因是服务没重启）。
    # 有测试钉住这一条。
    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(rest: str) -> Any:
        raise HTTPException(status_code=404, detail=f"没有这个接口：/api/{rest}")

    # -- 托管前端构建产物（存在才挂载）------------------------------------

    if STATIC_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _require(app: FastAPI, session_id: str) -> Session:
    session = app.state.store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期。")
    return session


def _client_key(request: Request, *, trust_proxy: bool) -> str:
    """限流用的客户端标识。

    部署在反向代理后面时 ``request.client.host`` 是代理 IP ——
    所有用户会被算成同一个人，限流要么误伤全员、要么形同虚设。

    但 ``X-Forwarded-For`` 可被伪造，因此只在明确配置了 ``WEB_TRUST_PROXY=1``
    （即确认前置了可信代理）时才采信。
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return "unknown"


async def _janitor_loop(app: FastAPI, interval: float) -> None:
    """后台清理：回收超时会话 + 清掉已完成任务的引用。

    没有它的话，TTL 只在「有新会话创建」时才生效 —— 服务空闲时
    过期会话会一直留在内存里。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            app.state.store.evict()
            for session_id, task in list(app.state.tasks.items()):
                if task.done():
                    app.state.tasks.pop(session_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 清理失败不该拖垮服务
            pass


def _revision_history(session: Session) -> list[dict[str, Any]]:
    """构造回灌给模型的上下文：原任务 + 上一版的完整输出。

    给的是 ``raw_text``（Markdown 正文 + JSON 块）而不是 ``plan`` 的纯 JSON：
    模型看得到上一版「长什么样」，改起来才像**改**而不是重写。
    只给结构化 JSON 的话，措辞、阶段命名这些它都会重新发挥一遍。

    拿不到上一版文本就返回空列表 —— 那时修订退化成一次普通规划，
    比塞一个空的 assistant 轮次进去要好。
    """
    previous = session.result or {}
    text = str(previous.get("raw_text") or previous.get("markdown") or "").strip()
    if not text:
        return []
    return [
        {"role": "user", "content": wrap_user_input(session.task)},
        {"role": "assistant", "content": text},
    ]


async def _settle_run(
    app: FastAPI,
    session: Session,
    orch: Orchestrator,
    *,
    source: str,
    feedback: str = "",
) -> None:
    """一次运行的收尾：登记版本 + 判定状态 + 写运行记录。

    **初版与修订共用这一条路径。** 两处逻辑一旦分叉，迟早会出现
    「修订路径忘了登记版本」或「状态判定不一致」这类只在某一条路径上复现的 bug，
    而这类 bug 最难查 —— 因为两条路径单独看都对。

    状态语义：

    - ``failed`` —— **基础设施**失败（LLM 抛异常、运行期崩溃），拿不到任何结果
    - ``done``   —— 运行正常结束。计划是否可用看 ``result.ok``
      （模型可能产出不合规 JSON 或含环的依赖图，那是模型的问题，不是服务的）

    注意 ``done`` 也可能对应 ``ok=false`` —— 版本链里照样记一笔，
    界面上标红，用户点一下就能切回上一版。
    """
    result = orch.last_result
    if result is not None:
        session.record_version(result.to_event_data(), feedback=feedback)
        # 运行记录：成功和失败都要记 —— 失败样本才是排查的重点
        record_run(
            result,
            session.task,
            source=source,
            model=app.state.settings.model,
            params=settings_params(app.state.settings),
        )

    if result is None:  # pragma: no cover - run_stream 必然产出结果
        session.error = "运行未产出结果"
        await session.set_status("failed")
    elif result.trace.output_status == "runtime_error":
        session.error = result.trace.error or "运行期异常"
        await session.set_status("failed")
    else:
        # 上一次可能留下过错误（比如上一轮修订失败），这次成功要把它清掉
        session.error = None
        await session.set_status("done")


async def _drive_session(app: FastAPI, session: Session) -> None:
    """后台驱动一次规划（初版），把事件写进会话。"""
    await session.set_status("running")
    try:
        llm = app.state.llm_factory(app.state.settings)
        orch = Orchestrator(
            llm,
            LocalToolRunner(),
            app.state.settings,
            ask_user=session.ask_user,
        )
        async for event in orch.run_stream(session.task):
            await session.emit(event)

        await _settle_run(app, session, orch, source="web")
    except asyncio.CancelledError:
        await session.set_status("failed")
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：驱动层自身出错也要反映到状态
        session.error = f"{type(exc).__name__}: {exc}"
        await session.emit(Event("error", {"message": session.error}))
        await session.set_status("failed")


async def _drive_revision(app: FastAPI, session: Session, feedback: str) -> None:
    """按用户反馈做一次**增量修订**。

    与 ``_drive_session`` 的差别只有两处：注入 ``history``（上一版计划）、
    以及把产出登记成一个新版本。**其余全走同一条 ``run_stream`` 管道** ——
    护栏（预算门控、DAG 校验、修复重试、注入检测）一个都不能少。
    开一条「直接改 JSON」的旁路，就等于给整个护栏体系留了个后门。

    本期刻意**不做证据复用**：上次查的天气/票价一律重查。
    慢一点，但绝对不会出现「用过时数据算出来的新计划」——
    那种错误没有任何测试能发现，而它恰好是这个项目一直在防的。
    """
    await session.set_status("running")
    try:
        llm = app.state.llm_factory(app.state.settings)
        orch = Orchestrator(
            llm,
            LocalToolRunner(),
            app.state.settings,
            ask_user=session.ask_user,
        )
        async for event in orch.run_stream(
            feedback,
            history=_revision_history(session),
            instruction=revision_instruction(feedback),
        ):
            await session.emit(event)

        await _settle_run(app, session, orch, source="revision", feedback=feedback)
    except asyncio.CancelledError:
        await session.set_status("failed")
        raise
    except Exception as exc:  # noqa: BLE001
        session.error = f"{type(exc).__name__}: {exc}"
        await session.emit(Event("error", {"message": session.error}))
        await session.set_status("failed")


# 供 `uv run uvicorn web.app:app` 直接使用
def _build_default_app() -> FastAPI:
    return create_app(
        rate_limit=int(os.getenv("WEB_RATE_LIMIT", "10")),
        trust_proxy=os.getenv("WEB_TRUST_PROXY") == "1",
    )


app = _build_default_app()
