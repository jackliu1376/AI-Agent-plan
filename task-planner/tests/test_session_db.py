"""会话持久化测试（``web/session_db.py`` + ``SessionStore`` 的磁盘后备）。

要守住的是四件事，每一件都对应一个真实会踩的坑：

1. **写进去能读回来** —— 落盘本身没丢字段。
2. **内存回收 ≠ 删除** —— TTL 到了只清内存，磁盘那行还在，点开还能看。
3. **重启后如实交代** —— 上次没跑完的会话标成 ``interrupted``，
   而不是继续显示「运行中」让用户干等。
4. **删掉的不复活** —— 取消会话是「先删行再取消任务」，
   而任务收到 CancelledError 后还会 ``set_status("failed")``，
   没拦住的话刚删的行会被写回来。

另外还有一条贯穿性约束：**任何落盘失败都不能抛异常**。
持久化是旁路，磁盘满了不该让正在跑的规划崩掉。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent.config import Settings
from agent.demo import ScriptedLLM
from tests.conftest import make_plan, plan_turn, tool_turn
from tests.test_web import collect_sse
from web.app import create_app
from web.session import (
    INTERRUPTED_MESSAGE,
    Session,
    SessionStore,
    _hydrate,
    session_ok,
)
from web.session_db import MAX_ROWS, SessionDB

WEATHER_ARGS = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}


def normal_script() -> list:
    return [tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)), plan_turn(make_plan())]


def _session(session_id: str = "s1", task: str = "成都 2 日游") -> Session:
    return Session(id=session_id, task=task)


# ---------------------------------------------------------------------------
# SessionDB：读写本身
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(session_db: SessionDB) -> None:
    session = _session()
    session.status = "done"
    session.events = [{"type": "plan_ready", "data": {"ok": True}}]
    session.result = {"ok": True, "step_count": 2, "plan": {"goal": "测试"}}

    assert session_db.save(session) is True

    record = session_db.load("s1")
    assert record is not None
    assert record["session_id"] == "s1"
    assert record["task"] == "成都 2 日游"
    assert record["status"] == "done"
    assert record["result"]["step_count"] == 2
    assert record["events"][0]["type"] == "plan_ready"
    assert record["ok"] is True


def test_save_is_upsert_not_append(session_db: SessionDB) -> None:
    """同一会话反复落盘只能有一行 —— 状态是在变的，不是新记录。"""
    session = _session()
    session_db.save(session)
    session.status = "running"
    session_db.save(session)
    session.status = "done"
    session.result = {"ok": True}
    session_db.save(session)

    assert session_db.count() == 1
    assert session_db.load("s1")["status"] == "done"


def test_list_orders_by_created_desc_and_respects_limit(session_db: SessionDB) -> None:
    for index in range(5):
        session = _session(f"s{index}", f"任务 {index}")
        session.created_wall = 1_000.0 + index
        session_db.save(session)

    rows = session_db.list(limit=3)
    assert [r["session_id"] for r in rows] == ["s4", "s3", "s2"]
    # 列表是摘要，不该把正文一起拖出来
    assert "events" not in rows[0]
    assert "result" not in rows[0]


def test_delete_removes_row(session_db: SessionDB) -> None:
    session_db.save(_session())
    assert session_db.delete("s1") is True
    assert session_db.delete("s1") is False, "重复删除应返回 False"
    assert session_db.load("s1") is None


def test_clear_empties_table(session_db: SessionDB) -> None:
    for index in range(3):
        session_db.save(_session(f"s{index}"))
    assert session_db.clear() == 3
    assert session_db.count() == 0


def test_prune_keeps_only_max_rows(session_db: SessionDB) -> None:
    """库不能无限长大：超上限后淘汰最旧的。"""
    for index in range(MAX_ROWS + 5):
        session = _session(f"s{index:04d}")
        session.created_wall = float(index)
        session_db.save(session)

    assert session_db.count() == MAX_ROWS
    assert session_db.load("s0000") is None, "最旧的应该被淘汰"
    assert session_db.load(f"s{MAX_ROWS + 4:04d}") is not None, "最新的必须还在"


def test_bad_path_returns_false_instead_of_raising(tmp_path) -> None:
    """落盘失败只能返回 False。磁盘问题不该让正在跑的规划崩掉。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
    db = SessionDB(blocker / "sessions.db")  # mkdir 必然失败

    assert db.save(_session()) is False
    assert db.load("s1") is None
    assert db.list() == []
    assert db.delete("s1") is False
    assert db.mark_interrupted("x") == 0


def test_corrupted_json_columns_fall_back(session_db: SessionDB) -> None:
    """坏行不能让整个文件读不出来（与 runs.jsonl 同一条原则）。"""
    session_db.save(_session())
    with session_db._connect() as conn:  # noqa: SLF001 - 测试需要制造脏数据
        conn.execute("UPDATE sessions SET events = '{不是 JSON', result = '###'")
        conn.commit()

    record = session_db.load("s1")
    assert record is not None
    assert record["events"] == []
    assert record["result"] is None


# ---------------------------------------------------------------------------
# 中断标记
# ---------------------------------------------------------------------------


def test_mark_interrupted_only_touches_live_states(session_db: SessionDB) -> None:
    for index, status in enumerate(["pending", "running", "awaiting_input", "done", "failed"]):
        session = _session(f"s{index}")
        session.status = status  # type: ignore[assignment]
        session_db.save(session)

    assert session_db.mark_interrupted("被中断了") == 3

    assert session_db.load("s0")["status"] == "interrupted"
    assert session_db.load("s1")["status"] == "interrupted"
    assert session_db.load("s2")["status"] == "interrupted"
    assert session_db.load("s3")["status"] == "done", "终态不该被改写"
    assert session_db.load("s4")["status"] == "failed"


# ---------------------------------------------------------------------------
# SessionStore：内存 + 磁盘协同
# ---------------------------------------------------------------------------


def test_store_persists_on_status_change(session_db: SessionDB) -> None:
    """状态跃迁就要落盘，不用等会话结束。"""
    store = SessionStore(db=session_db)
    session = store.create("成都 2 日游")
    assert session_db.load(session.id)["status"] == "pending"

    session.status = "running"
    session._persist()  # noqa: SLF001 - 直接驱动，省去跑一遍事件循环
    assert session_db.load(session.id)["status"] == "running"


async def test_set_status_persists(session_db: SessionDB) -> None:
    store = SessionStore(db=session_db)
    session = store.create("成都 2 日游")
    await session.set_status("running")
    await session.set_status("done")
    assert session_db.load(session.id)["status"] == "done"


def test_store_evict_keeps_disk_row(session_db: SessionDB) -> None:
    """核心语义：TTL 回收只清内存，记录本身还在，还能点开。"""
    store = SessionStore(ttl_seconds=60.0, db=session_db)
    first = store.create("任务一")
    store.create("任务二")

    # 模拟「这条会话很久没人看了」
    first.updated_at -= 3600
    assert store.evict() >= 1
    assert store.size == 1, "过期的那条应该已经不在内存里"

    # 关键断言：磁盘上那一行没有因为内存回收而消失
    assert session_db.load(first.id) is not None

    # 而且还能重新捞回内存
    restored = store.get(first.id)
    assert restored is not None
    assert restored.task == "任务一"
    assert store.size == 2


def test_store_get_hydrates_from_disk(session_db: SessionDB) -> None:
    """模拟重启：新 store 共用同一个库，旧会话照样打得开。"""
    writer = SessionStore(db=session_db)
    session = writer.create("毕业论文帮我做")
    session.status = "done"
    session.result = {"ok": True, "step_count": 3}
    session.events = [{"type": "plan_ready", "data": {"ok": True}}]
    session._persist()  # noqa: SLF001

    reader = SessionStore(db=session_db)  # 全新的内存表，等价于重启
    revived = reader.get(session.id)
    assert revived is not None
    assert revived.task == "毕业论文帮我做"
    assert revived.status == "done"
    assert revived.result == {"ok": True, "step_count": 3}


def test_store_drop_removes_disk_row(session_db: SessionDB) -> None:
    store = SessionStore(db=session_db)
    session = store.create("任务")
    assert store.drop(session.id) is True
    assert session_db.load(session.id) is None


def test_dropped_session_cannot_be_resurrected(session_db: SessionDB) -> None:
    """回归：取消 = 先 drop 再 cancel 任务，而任务随后还会 set_status("failed")。

    没拦住的话，刚删掉的行会被这一下写回磁盘 —— 用户删了又冒出来。
    """
    store = SessionStore(db=session_db)
    session = store.create("任务")
    store.drop(session.id)

    session._persist()  # noqa: SLF001 - 模拟被取消的任务收尾时的那次写盘
    assert session_db.load(session.id) is None, "已删除的会话被写回来了"


def test_store_list_without_db_falls_back_to_memory() -> None:
    """没有持久化时列表退化成内存视图，不能直接报错。"""
    store = SessionStore()
    store.create("任务一")
    store.create("任务二")
    rows = store.list(limit=10)
    assert len(rows) == 2
    assert {r["task"] for r in rows} == {"任务一", "任务二"}
    assert all(r["status"] == "pending" for r in rows)


# ---------------------------------------------------------------------------
# 磁盘记录 -> Session（_hydrate）
# ---------------------------------------------------------------------------


def test_hydrate_downgrades_live_status_to_interrupted() -> None:
    session = _hydrate(
        {
            "session_id": "s1",
            "task": "上次没跑完",
            "status": "running",
            "created_at": time.time(),
            "events": [{"type": "run_started", "data": {}}],
            "result": None,
            "error": None,
            "clarification": None,
        }
    )
    assert session.status == "interrupted"
    assert session.error == INTERRUPTED_MESSAGE
    assert session.is_finished is True


def test_hydrate_appends_terminal_event_when_missing() -> None:
    """缺终止事件的话，重连的前端会一直转圈直到重试上限。

    这里补一条 error 事件，让流能自己收尾。
    """
    session = _hydrate(
        {
            "session_id": "s1",
            "task": "任务",
            "status": "failed",
            "created_at": time.time(),
            "events": [{"type": "tool_call", "data": {}}],
            "result": None,
            "error": "上游炸了",
            "clarification": None,
        }
    )
    assert session.events[-1]["type"] == "error"
    assert session.events[-1]["data"]["message"] == "上游炸了"


def test_hydrate_does_not_duplicate_existing_terminal_event() -> None:
    session = _hydrate(
        {
            "session_id": "s1",
            "task": "任务",
            "status": "done",
            "created_at": time.time(),
            "events": [{"type": "plan_ready", "data": {"ok": True}}],
            "result": {"ok": True},
            "error": None,
            "clarification": None,
        }
    )
    assert len(session.events) == 1


def test_hydrate_tolerates_garbage_events() -> None:
    session = _hydrate(
        {
            "session_id": "s1",
            "task": "任务",
            "status": "done",
            "created_at": time.time(),
            "events": ["我不是字典", None, {"type": "plan_ready", "data": {}}],
            "result": None,
            "error": None,
            "clarification": None,
        }
    )
    assert [e["type"] for e in session.events] == ["plan_ready"]


# ---------------------------------------------------------------------------
# session_ok：三种取值不能塌成 bool
# ---------------------------------------------------------------------------


def test_session_ok_keeps_unknown_distinct_from_failed() -> None:
    running = _session()
    running.status = "running"
    assert session_ok(running) is None, "还在跑 —— 前端该显示灰点，不是红点"

    failed = _session()
    failed.status = "failed"
    assert session_ok(failed) is False

    interrupted = _session()
    interrupted.status = "interrupted"
    assert session_ok(interrupted) is False

    good = _session()
    good.status = "done"
    good.result = {"ok": True}
    assert session_ok(good) is True

    bad = _session()
    bad.status = "done"
    bad.result = {"ok": False}
    assert session_ok(bad) is False

    # done 但压根没产出计划（模型输出不合规）
    empty = _session()
    empty.status = "done"
    empty.result = None
    assert session_ok(empty) is False


# ---------------------------------------------------------------------------
# HTTP 层：列表 / 恢复 / 重启 / 删除
# ---------------------------------------------------------------------------


def _build(settings: Settings, db: SessionDB | None, script=normal_script):
    def factory(_settings: Settings) -> Any:
        return ScriptedLLM(script())

    return create_app(settings=settings, db=db, llm_factory=factory, rate_limit=100)


async def _run_task(client: AsyncClient, task: str = "成都 2 日游") -> str:
    sid = (await client.post("/api/sessions", json={"task": task})).json()["session_id"]
    await collect_sse(client, f"/api/sessions/{sid}/events", stop_at={"plan_ready"})
    return sid


async def test_list_endpoint_returns_history(settings: Settings, session_db: SessionDB) -> None:
    app = _build(settings, session_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/sessions")).json() == {"count": 0, "sessions": []}

        sid = await _run_task(client, "毕业论文帮我做")
        body = (await client.get("/api/sessions")).json()

    assert body["count"] == 1
    row = body["sessions"][0]
    assert row["session_id"] == sid
    assert row["task"] == "毕业论文帮我做"
    assert row["status"] == "done"
    assert row["ok"] is True
    assert row["created_at"] > 0


async def test_list_endpoint_respects_limit(settings: Settings, session_db: SessionDB) -> None:
    app = _build(settings, session_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for index in range(3):
            await _run_task(client, f"任务 {index}")

        body = (await client.get("/api/sessions?limit=2")).json()

    assert body["count"] == 2
    assert [r["task"] for r in body["sessions"]] == ["任务 2", "任务 1"]

    # 参数越界要被拒（ge=1, le=100）
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/sessions?limit=0")).status_code == 422


async def test_session_survives_restart(settings: Settings, session_db: SessionDB) -> None:
    """本次改造的核心承诺：重启服务后，「最近」里的记录还打得开。"""
    first = _build(settings, session_db)
    async with AsyncClient(transport=ASGITransport(app=first), base_url="http://test") as client:
        sid = await _run_task(client)

    # 换一个全新的 app（内存全空），共用同一个库 —— 等价于重启服务
    second = _build(settings, session_db)
    async with AsyncClient(transport=ASGITransport(app=second), base_url="http://test") as client:
        view = (await client.get(f"/api/sessions/{sid}")).json()
        listing = (await client.get("/api/sessions")).json()

    assert view["status"] == "done"
    assert view["result"]["ok"] is True
    assert view["result"]["plan"]["goal"] == "测试计划"
    assert view["error"] is None
    # 事件也要回来，否则恢复出来的页面比刚跑完时少一块「已查证」
    assert any(e["type"] == "tool_result" for e in view["events"])
    assert listing["sessions"][0]["session_id"] == sid


async def test_startup_marks_orphans_interrupted(settings: Settings, session_db: SessionDB) -> None:
    """上次进程留下的「运行中」会话，启动时必须被如实标成中断。

    不处理的话，「最近」里会出现一条永远转圈的僵尸记录。
    """
    orphan = Session(id="orphan01", task="上次没跑完的任务")
    orphan.status = "running"
    session_db.save(orphan)

    app = _build(settings, session_db)
    # httpx 的 ASGITransport 不会自己跑 lifespan，这里显式进入
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            listing = (await client.get("/api/sessions")).json()
            view = (await client.get("/api/sessions/orphan01")).json()

    assert listing["sessions"][0]["status"] == "interrupted"
    assert listing["sessions"][0]["ok"] is False
    assert view["status"] == "interrupted"
    assert INTERRUPTED_MESSAGE in view["error"]
    # 事件流能自己收尾 —— 否则重连的前端会一直等到重试上限
    assert view["events"][-1]["type"] == "error"


async def test_delete_endpoint_removes_from_disk(
    settings: Settings, session_db: SessionDB
) -> None:
    app = _build(settings, session_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = await _run_task(client)
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
        assert (await client.get(f"/api/sessions/{sid}")).status_code == 404
        assert (await client.get("/api/sessions")).json()["count"] == 0

    assert session_db.load(sid) is None, "删除接口必须同时清掉磁盘那一行"


async def test_cancelled_session_does_not_come_back(
    settings: Settings, session_db: SessionDB
) -> None:
    """取消（DELETE）后，后台任务收尾时的写盘不能把记录带回来。"""

    class SlowLLM:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def chat(self, messages, tools=None):  # noqa: ANN001
            time.sleep(0.3)
            return self._inner.chat(messages, tools=tools)

    app = create_app(
        settings=settings,
        db=session_db,
        llm_factory=lambda _s: SlowLLM(ScriptedLLM(normal_script())),
        rate_limit=100,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = (await client.post("/api/sessions", json={"task": "成都 2 日游"})).json()[
            "session_id"
        ]
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 200
        # 给被取消的任务一点时间跑完它的收尾路径
        import asyncio

        await asyncio.sleep(0.5)
        assert (await client.get("/api/sessions")).json()["count"] == 0

    assert session_db.load(sid) is None


async def test_disabled_db_keeps_pure_memory(settings: Settings, monkeypatch) -> None:
    """``SESSION_DB_ENABLED=0`` 时行为退回改造前：只在内存里。"""
    monkeypatch.setenv("SESSION_DB_ENABLED", "0")
    app = create_app(
        settings=settings,
        llm_factory=lambda _s: ScriptedLLM(normal_script()),
        rate_limit=100,
    )
    assert app.state.store._db is None  # noqa: SLF001

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = await _run_task(client)
        assert (await client.get("/api/sessions")).json()["count"] == 1
        # 换一个 app 就查不到了 —— 这正是关掉持久化后的语义
        other = create_app(
            settings=settings,
            llm_factory=lambda _s: ScriptedLLM(normal_script()),
            rate_limit=100,
        )
        async with AsyncClient(
            transport=ASGITransport(app=other), base_url="http://test"
        ) as second:
            assert (await second.get(f"/api/sessions/{sid}")).status_code == 404


async def test_persist_failure_does_not_break_run(
    settings: Settings, tmp_path, monkeypatch
) -> None:
    """磁盘坏了也不能影响规划本身 —— 落盘是旁路。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("占位", encoding="utf-8")
    broken = SessionDB(blocker / "sessions.db")

    app = _build(settings, broken)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sid = await _run_task(client)
        view = (await client.get(f"/api/sessions/{sid}")).json()
        listing = (await client.get("/api/sessions")).json()

    assert view["status"] == "done"
    assert view["result"]["ok"] is True
    # 列表读不出来只能是空的，不能是 500
    assert listing == {"count": 0, "sessions": []}


@pytest.mark.parametrize("status", ["pending", "running", "awaiting_input"])
def test_live_statuses_are_the_three_non_terminal_ones(status: str) -> None:
    from web.session import FINISHED_STATUSES, LIVE_STATUSES

    assert status in LIVE_STATUSES
    assert status not in FINISHED_STATUSES
