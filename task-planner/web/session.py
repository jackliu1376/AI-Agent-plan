"""会话管理 —— 把一次规划包装成「可订阅、可暂停恢复」的会话。

为什么需要会话
--------------
Web 场景和 CLI 有三个根本差异：

1. **流式订阅**：客户端可能晚几秒才连上 SSE，也可能刷新页面重连。
   因此事件必须**保留在会话里**（而不是丢进一次性队列），客户端带游标重放。
2. **澄清要跨请求**：提问走 SSE 推给前端，回答走另一个 HTTP 请求回来。
   中间需要一个 ``Future`` 把协程挂起。
3. **状态要能查询**：刷新页面后得能拿回最终结果，不能只靠流。

并发模型
--------
每个会话持有**独立的 Orchestrator 实例**——因为 ``Orchestrator._last_result``
是实例级状态，不支持同一实例并发跑多个任务。

事件订阅用 ``asyncio.Condition`` + 列表 + 游标实现，天然支持：
- 多个订阅者（虽然当前场景只有一个）
- 断线重连后从头重放
- 心跳保活（避免代理掐断空闲连接）

内存与磁盘
----------
会话有**两份**：内存里的活对象（跑得快、能挂协程、能推事件），
和磁盘上的行（``web/session_db.py``，跑得慢但重启还在）。

分工是这样的：

- **内存是主，磁盘是备份。** 事件推送、澄清挂起全走内存。
- **状态跃迁时写盘**（不是每个事件都写），见 ``Session._persist``。
- **内存未命中就回磁盘捞。** 于是「重启后点开一条旧记录」也成立。
- **TTL 回收只清内存，不删盘。** 磁盘有自己的容量上限（``MAX_ROWS``）。
  两者语义不同：内存回收是「暂时不用了」，删除是「用户不要了」。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.events import Event

SessionStatus = Literal[
    "pending",
    "running",
    "awaiting_input",
    "done",
    "failed",
    # 进程重启 / 崩溃导致没跑完。不是模型的问题，也不是用户取消，
    # 单独一个状态是为了如实说明原因，而不是含糊地报「失败」。
    "interrupted",
]

# 非终态：进程退出时若还停在这些状态，说明这次运行被中断了。
LIVE_STATUSES: frozenset[str] = frozenset({"pending", "running", "awaiting_input"})

FINISHED_STATUSES: frozenset[str] = frozenset({"done", "failed", "interrupted"})

# 事件流里表示「可以收尾」的类型。重放历史事件时必须有其中一条，
# 否则重连的前端会一直等下去（它靠这两个事件判断任务结束）。
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset({"plan_ready", "error"})

INTERRUPTED_MESSAGE = "服务重启，这次规划被中断了。"

# 心跳间隔（秒）。SSE 连接空闲太久会被中间代理掐断，定期发注释行保活。
HEARTBEAT_SECONDS = 15.0

# 会话保留时长（秒）。超过后由清理任务回收，防止内存无限增长。
SESSION_TTL_SECONDS = 30 * 60

# 等待用户回答澄清的最长时间（秒）。
# 没有它的话，用户提问后直接关掉页面会让协程永久挂在 Future 上。
CLARIFY_TIMEOUT_SECONDS = 10 * 60

# 用户超时未答时回灌给模型的兜底指令
CLARIFY_TIMEOUT_REPLY = (
    "用户在规定时间内没有回答。请基于合理假设继续规划，"
    "并在 assumptions 中明确列出你补充了哪些假设。"
)


@dataclass
class Session:
    """一次规划任务的完整状态。"""

    id: str
    task: str
    status: SessionStatus = "pending"
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    # 墙上时钟。TTL 用 monotonic（不受系统改时间影响，是正确做法），
    # 但「最近」列表要按真实时间排序并展示给用户，只能存 time.time()。
    # 两个时间戳各司其职，不要合并。
    created_wall: float = field(default_factory=time.time)

    # 事件按顺序保留，供晚连上 / 重连的客户端重放
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None

    # 当前待用户回答的问题（供前端渲染）
    clarification: dict[str, Any] | None = None

    # 用户超时未答（此时 run 会带着「按假设继续」的指令恢复）
    timed_out: bool = False

    # 已被显式删除。置位后禁止再落盘 —— 取消是「先 drop 再 cancel 任务」，
    # 而任务收到 CancelledError 后还会 set_status("failed")，
    # 不拦的话会把刚删掉的那一行又写回去。
    dropped: bool = False

    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    _answer_future: asyncio.Future[str] | None = field(default=None, repr=False)
    _early_answer: str | None = field(default=None, repr=False)
    # 由 SessionStore 注入：把当前状态写盘。不注入就纯内存运行。
    _on_change: Callable[[], None] | None = field(default=None, repr=False)

    # -- 状态 -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.updated_at

    async def set_status(self, status: SessionStatus) -> None:
        """更新状态并唤醒所有等待中的订阅者。"""
        async with self._condition:
            self.status = status
            self.updated_at = time.monotonic()
            self._condition.notify_all()
        # 落盘放在锁外：持锁时做磁盘 I/O 会把所有订阅者一起卡住。
        self._persist()

    def _persist(self) -> None:
        """把当前状态同步给持有者。**绝不抛异常**。

        只在状态跃迁时调用（``set_status``），不在每个事件上调用：
        一次运行有 10–30 个事件，每个都写盘是明显的写放大，
        而状态跃迁已经覆盖了所有「崩溃后再看要有意义」的时点。
        """
        if self.dropped or self._on_change is None:
            return
        try:
            self._on_change()
        except Exception:  # noqa: BLE001 - 持久化是旁路，不能影响主流程
            pass

    # -- 事件 -------------------------------------------------------------

    async def emit(self, event: Event) -> None:
        """记录一个事件并唤醒订阅者。"""
        async with self._condition:
            self.events.append(event.to_dict())
            self.updated_at = time.monotonic()
            if event.type == "plan_ready":
                self.result = event.data
            self._condition.notify_all()

    async def stream(self, cursor: int = 0) -> AsyncIterator[dict[str, Any] | None]:
        """从 ``cursor`` 开始产出事件；``None`` 表示心跳。

        客户端断线重连时把已收到的条数作为 ``cursor`` 传回来即可续传，
        传 0 则从头重放。
        """
        while True:
            async with self._condition:
                if cursor >= len(self.events) and not self.is_finished:
                    # 没有新事件就等一会儿，超时后发心跳
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(), timeout=HEARTBEAT_SECONDS
                        )
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
                batch = self.events[cursor:]
                cursor = len(self.events)
                finished = self.is_finished

            for item in batch:
                yield item

            if finished and cursor >= len(self.events):
                return
            if not batch:
                yield None  # 心跳

    # -- 澄清：挂起与唤醒 -------------------------------------------------

    async def ask_user(self, payload: dict[str, Any]) -> str:
        """作为 ``Orchestrator`` 的 ``ask_user`` 回调：挂起直到前端提交答案。"""
        self.clarification = {
            "question": payload.get("question", ""),
            "options": payload.get("options", []),
            "reason": payload.get("reason", ""),
        }
        await self.set_status("awaiting_input")

        # 处理「答案比 Future 先到」的竞态：SSE 推送和 HTTP 提交是两条独立链路，
        # 前端可能在协程真正挂起之前就把答案发回来了。
        if self._early_answer is not None:
            answer, self._early_answer = self._early_answer, None
            self.clarification = None
            await self.set_status("running")
            return answer

        loop = asyncio.get_running_loop()
        self._answer_future = loop.create_future()
        try:
            return await asyncio.wait_for(
                self._answer_future, timeout=CLARIFY_TIMEOUT_SECONDS
            )
        except (TimeoutError, asyncio.TimeoutError):
            # 用户不回答不能让协程永久挂起 —— 降级为「按假设继续」，
            # 与「没有交互通道」时的行为保持一致。
            self.timed_out = True
            return CLARIFY_TIMEOUT_REPLY
        finally:
            self._answer_future = None
            self.clarification = None
            await self.set_status("running")

    def submit_answer(self, text: str) -> bool:
        """前端提交澄清答案。返回是否被接受。"""
        future = self._answer_future
        if future is None:
            if self.status != "awaiting_input":
                return False
            self._early_answer = text
            return True
        if future.done():
            return False
        future.set_result(text)
        return True


class SessionStore:
    """内存会话表 + TTL 回收 + 可选的磁盘后备。

    单机够用。要多实例部署时把内存表换成 Redis 即可，接口不变
    （但那时磁盘后备也得换成共享存储，否则多实例各写各的库）。

    ``db`` 不传就是纯内存 —— 测试和 ``SESSION_DB_ENABLED=0`` 走这条路。

    ``on_evict`` 回调很关键：**回收会话时必须同时取消它的后台任务**。
    只删字典条目的话，卡在 ``awaiting_input`` 的协程仍挂在 Future 上永不退出，
    形成协程 + 内存双重泄漏。

    「回收」与「删除」是两件事，别混：

    - ``_evict`` 只从内存摘掉（TTL / 超上限）。磁盘那行留着，
      下次 ``get`` 还能捞回来。
    - ``drop`` 是用户明确不要了，内存和磁盘一起清。
    """

    def __init__(
        self,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        max_sessions: int = 200,
        on_evict: Callable[[str], None] | None = None,
        db: Any | None = None,
    ):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._on_evict = on_evict
        self._db = db

    def set_on_evict(self, callback: Callable[[str], None] | None) -> None:
        """注册回收钩子（在 store 创建之后才能拿到 app.state，所以单独设）。"""
        self._on_evict = callback

    def evict(self) -> int:
        """主动执行一次回收（供后台清理任务调用）。返回被回收的会话数。"""
        return self._evict()

    def create(self, task: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:16], task=task)
        session._on_change = lambda: self._save(session)
        self._sessions[session.id] = session
        self._evict()
        # 立刻落一行：即便下一秒进程被杀，这条任务也不会凭空消失。
        # 状态跃迁也会写，但「刚提交就崩」是最常见的一种崩。
        self._save(session)
        return session

    def get(self, session_id: str) -> Session | None:
        """先查内存，未命中再回磁盘捞。"""
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        if self._db is None:
            return None

        record = self._db.load(session_id)
        if record is None:
            return None

        session = _hydrate(record)
        session._on_change = lambda: self._save(session)
        self._sessions[session.id] = session
        self._evict()
        return session

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """「最近」列表。有磁盘就以磁盘为准（它是全量的）。"""
        if self._db is not None:
            return self._db.list(limit)

        ordered = sorted(
            self._sessions.values(), key=lambda s: s.created_wall, reverse=True
        )
        size = max(1, limit)
        return [
            {
                "session_id": s.id,
                "task": s.task,
                "status": s.status,
                "created_at": s.created_wall,
                "updated_at": s.created_wall,
                "ok": session_ok(s),
            }
            for s in ordered[:size]
        ]

    def mark_interrupted(self) -> int:
        """启动时把上次遗留的非终态会话标成中断。返回处理条数。"""
        if self._db is None:
            return 0
        return self._db.mark_interrupted(INTERRUPTED_MESSAGE)

    def drop(self, session_id: str) -> bool:
        """彻底删除（内存 + 磁盘）。返回是否真的删掉了什么。"""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            # 置位后再取消任务：否则任务收到 CancelledError 后的
            # set_status("failed") 会把这一行重新写回磁盘。
            session.dropped = True

        deleted = self._db.delete(session_id) if self._db is not None else False
        if session is not None or deleted:
            self._notify_evicted(session_id)
            return True
        return False

    # -- 内部 -------------------------------------------------------------

    def _save(self, session: Session) -> None:
        if self._db is None or session.dropped:
            return
        self._db.save(session)  # SessionDB.save 自己吞异常

    def _notify_evicted(self, session_id: str) -> None:
        if self._on_evict is not None:
            try:
                self._on_evict(session_id)
            except Exception:  # noqa: BLE001 - 钩子失败不该影响回收本身
                pass

    def _evict(self) -> int:
        """回收超时会话；若仍超出上限，按最旧优先淘汰。

        **只清内存，不动磁盘。** 磁盘的容量由 ``SessionDB.MAX_ROWS`` 管，
        语义是「太久远的记录可以丢」；而内存回收的语义是「这段时间没人看，
        先放掉」—— 两者不是一回事。
        """
        stale = [sid for sid, s in self._sessions.items() if s.age_seconds > self._ttl]
        for sid in stale:
            del self._sessions[sid]
            self._notify_evicted(sid)

        overflow = len(self._sessions) - self._max
        if overflow > 0:
            oldest = sorted(self._sessions.values(), key=lambda s: s.created_at)
            for session in oldest[:overflow]:
                del self._sessions[session.id]
                self._notify_evicted(session.id)

        return len(stale) + max(overflow, 0)

    @property
    def size(self) -> int:
        return len(self._sessions)


# ---------------------------------------------------------------------------
# 磁盘记录 -> Session
# ---------------------------------------------------------------------------


def session_ok(session: Session) -> bool | None:
    """这次规划成功了没有。``None`` 表示**还没结论**（还在跑）。

    三种取值都要保留，不能塌成 bool：

    - ``True``  跑完且产出了合规计划
    - ``False`` 跑完了但没成功，或者压根没跑完（failed / interrupted）
    - ``None``  还在跑 —— 前端该显示灰色圆点，而不是红色
    """
    if session.status in ("failed", "interrupted"):
        return False
    if session.status != "done":
        return None
    result = session.result
    if isinstance(result, dict) and isinstance(result.get("ok"), bool):
        return result["ok"]
    # done 但没产出计划（模型输出不合规）—— 算没成功
    return False


def _hydrate(record: dict[str, Any]) -> Session:
    """把一行磁盘记录还原成可用的 Session。

    两个必须处理的细节：

    1. **非终态一律降级为 interrupted。** 磁盘上写着 running，但持有它的
       进程已经没了（否则内存里就该有它）。如实说「被中断了」，
       比假装还在跑要好 —— 后者会让用户一直等一个永远不会来的结果。
    2. **补一条终止事件。** 前端靠 plan_ready / error 判断「可以收尾了」。
       缺了它，重连会一直重试到上限，最后报一句「连接中断」，
       用户根本不知道发生了什么。
    """
    events = [e for e in (record.get("events") or []) if isinstance(e, dict)]
    status: str = record.get("status") or "failed"
    error = record.get("error")

    if status in LIVE_STATUSES:
        status = "interrupted"
        error = error or INTERRUPTED_MESSAGE

    if status in FINISHED_STATUSES and not any(
        e.get("type") in TERMINAL_EVENT_TYPES for e in events
    ):
        events.append(Event("error", {"message": error or INTERRUPTED_MESSAGE}).to_dict())

    return Session(
        id=record["session_id"],
        task=record.get("task", ""),
        status=status,  # type: ignore[arg-type]
        created_wall=float(record.get("created_at") or time.time()),
        events=events,
        result=record.get("result"),
        error=error,
        clarification=record.get("clarification"),
    )
