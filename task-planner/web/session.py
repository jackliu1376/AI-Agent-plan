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
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.events import Event

SessionStatus = Literal["pending", "running", "awaiting_input", "done", "failed"]

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

    # 事件按顺序保留，供晚连上 / 重连的客户端重放
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None

    # 当前待用户回答的问题（供前端渲染）
    clarification: dict[str, Any] | None = None

    # 用户超时未答（此时 run 会带着「按假设继续」的指令恢复）
    timed_out: bool = False

    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    _answer_future: asyncio.Future[str] | None = field(default=None, repr=False)
    _early_answer: str | None = field(default=None, repr=False)

    # -- 状态 -------------------------------------------------------------

    @property
    def is_finished(self) -> bool:
        return self.status in ("done", "failed")

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.updated_at

    async def set_status(self, status: SessionStatus) -> None:
        """更新状态并唤醒所有等待中的订阅者。"""
        async with self._condition:
            self.status = status
            self.updated_at = time.monotonic()
            self._condition.notify_all()

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
    """内存会话表 + TTL 回收。

    单机够用。要多实例部署时把这里换成 Redis 即可，接口不变。

    ``on_evict`` 回调很关键：**回收会话时必须同时取消它的后台任务**。
    只删字典条目的话，卡在 ``awaiting_input`` 的协程仍挂在 Future 上永不退出，
    形成协程 + 内存双重泄漏。
    """

    def __init__(
        self,
        ttl_seconds: float = SESSION_TTL_SECONDS,
        max_sessions: int = 200,
        on_evict: Callable[[str], None] | None = None,
    ):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._on_evict = on_evict

    def set_on_evict(self, callback: Callable[[str], None] | None) -> None:
        """注册回收钩子（在 store 创建之后才能拿到 app.state，所以单独设）。"""
        self._on_evict = callback

    def evict(self) -> int:
        """主动执行一次回收（供后台清理任务调用）。返回被回收的会话数。"""
        return self._evict()

    def create(self, task: str) -> Session:
        session = Session(id=uuid.uuid4().hex[:16], task=task)
        self._sessions[session.id] = session
        self._evict()
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: str) -> bool:
        removed = self._sessions.pop(session_id, None) is not None
        if removed:
            self._notify_evicted(session_id)
        return removed

    def _notify_evicted(self, session_id: str) -> None:
        if self._on_evict is not None:
            try:
                self._on_evict(session_id)
            except Exception:  # noqa: BLE001 - 钩子失败不该影响回收本身
                pass

    def _evict(self) -> int:
        """回收超时会话；若仍超出上限，按最旧优先淘汰。"""
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
