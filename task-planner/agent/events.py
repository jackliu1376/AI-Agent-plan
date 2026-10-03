"""编排循环对外产出的事件流定义。

为什么需要事件流
----------------
原来 ``Orchestrator.run()`` 是一次性返回最终结果，整个规划过程（10–60 秒）用户只能
盯着转圈。改成事件流后，前端可以实时显示"正在查天气…""已找到 10 个景点…"，
体验完全不同。

设计原则
--------
1. **``data`` 必须是 JSON 可序列化的** —— Web 层（SSE）可以直接把事件原样推给前端，
   不需要为每种事件类型写转换代码。这是刻意约束，不是巧合。
2. **事件类型保持精简** —— 前端只需区分三类：进度（可忽略）、需要用户回答（阻塞）、
   最终结果（结束）。
3. **向后兼容** —— ``Orchestrator.run()`` 变成事件流的消费者，对外行为不变，
   因此现有测试无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    "run_started",      # 一次规划开始
    "turn_started",     # 进入第 N 轮模型调用
    "tool_call",        # 模型请求调用某个工具
    "tool_result",      # 工具返回（成功或失败）
    "clarification",    # 需要用户补充信息（前端应弹出提问并等待）
    "repair",           # 输出不合规，触发一次修复重试
    "warning",          # 非致命问题（如输出被长度上限截断）
    "plan_ready",       # 产出最终计划
    "error",            # 运行期异常
]


@dataclass
class Event:
    """一次事件。``data`` 保证 JSON 可序列化。"""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}

    def summary(self) -> str:
        """给 CLI 用的一行人话摘要。"""
        d = self.data
        match self.type:
            case "run_started":
                return f"开始规划（轮次上限 {d.get('max_turns')}，工具预算 {d.get('tool_budget')}）"
            case "turn_started":
                return f"第 {d.get('turn')}/{d.get('max_turns')} 轮：请求模型"
            case "tool_call":
                return f"调用工具 {d.get('tool')} {d.get('args')}"
            case "tool_result":
                flag = "✅" if d.get("ok") else f"❌ {d.get('error_code')}"
                cached = "（缓存）" if d.get("cached") else ""
                return f"{flag} {d.get('tool')}{cached} {d.get('latency_ms')}ms"
            case "clarification":
                return f"需要用户补充信息：{d.get('question')}"
            case "repair":
                return f"输出不合规，触发修复重试：{d.get('reason')}"
            case "warning":
                return f"⚠️ {d.get('message')}"
            case "plan_ready":
                return f"计划就绪（{d.get('step_count')} 个步骤）"
            case "error":
                return f"运行出错：{d.get('message')}"
        return self.type
