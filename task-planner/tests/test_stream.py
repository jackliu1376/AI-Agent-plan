"""事件流（streaming）测试。

背景
----
为了让 Web 前端能实时展示进度，``Orchestrator`` 从「一次性返回」改成了
「产出事件流」：

- ``run_stream()`` —— 异步生成器，逐个产出 ``Event``（Web/SSE 直接消费）
- ``run_async()`` —— 消费事件流，返回最终 ``PlanResult``
- ``run()`` —— 同步薄封装

本文件锁住三件事：
1. 事件序列完整、类型正确；
2. **``data`` 必须 JSON 可序列化**（Web 层要把事件原样推给前端，不能有例外）；
3. 澄清中断在事件流里能正确「产出事件 → 挂起 → 回填答案」，
   且同步 / 异步 ``ask_user`` 都支持。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.config import Settings
from agent.demo import ScriptedLLM
from agent.events import Event
from agent.loop import Orchestrator
from agent.tool_runner import LocalToolRunner
from tests.conftest import make_plan, plan_turn, text_turn, tool_turn

WEATHER_ARGS = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}


def _simple_script():
    """一轮工具调用 + 一轮出计划。"""
    return ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)),
            plan_turn(make_plan()),
        ]
    )


async def _collect(orch: Orchestrator, task: str) -> list[Event]:
    return [event async for event in orch.run_stream(task)]


# ---------------------------------------------------------------------------
# 事件序列
# ---------------------------------------------------------------------------


async def test_stream_yields_complete_sequence(settings: Settings, fake_weather, runner) -> None:
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")
    types = [e.type for e in events]

    assert types[0] == "run_started"
    assert types[-1] == "plan_ready"
    assert types.count("turn_started") == 2
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1


async def test_run_started_carries_limits(settings: Settings, fake_weather, runner) -> None:
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    started = events[0]
    assert started.data["max_turns"] == settings.max_turns
    assert started.data["tool_budget"] == settings.tool_budget
    assert started.data["injection_flags"] == 0


async def test_tool_call_event_precedes_result(settings: Settings, fake_weather, runner) -> None:
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    call = next(e for e in events if e.type == "tool_call")
    result = next(e for e in events if e.type == "tool_result")
    assert events.index(call) < events.index(result)
    assert call.data["tool"] == "get_weather_forecast"
    assert call.data["args"]["city"] == "成都"
    assert result.data["tool"] == "get_weather_forecast"
    assert result.data["ok"] is True
    assert result.data["budget_used"] == 1


async def test_failed_tool_still_emits_result_event(
    settings: Settings, broken_weather, runner
) -> None:
    """工具失败也必须产出 tool_result 事件，前端才能显示"这一步没拿到数据"。"""
    orch = Orchestrator(
        ScriptedLLM(
            [tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)), plan_turn(make_plan())]
        ),
        runner,
        settings,
    )
    events = await _collect(orch, "成都 2 日游")

    result = next(e for e in events if e.type == "tool_result")
    assert result.data["ok"] is False
    assert result.data["error_code"] == "UPSTREAM_TIMEOUT"
    assert result.data["attempts"] == 2


async def test_tool_result_carries_error_message(
    settings: Settings, broken_weather, runner
) -> None:
    """失败事件必须带上**错误原文**，不能只给错误码。

    前端原来只拿得到 `error_code`，于是把后端的详细说明压成「参数不合法」
    这种四字标签 —— 用户看到「查天气参数不合法」完全不知道哪里不对，
    而后端其实写了「预报只覆盖到 10-23，请改用区间内的日期」这种可执行的话。
    """
    orch = Orchestrator(
        ScriptedLLM(
            [tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)), plan_turn(make_plan())]
        ),
        runner,
        settings,
    )
    events = await _collect(orch, "成都 2 日游")

    result = next(e for e in events if e.type == "tool_result")
    message = result.data["error_message"]
    assert message, "失败事件没带 error_message，前端只能显示错误码标签"
    assert len(message) > 10, f"错误原文太短，不像是给人看的：{message!r}"


async def test_successful_tool_result_has_no_error_message(
    settings: Settings, fake_weather, runner
) -> None:
    """成功的调用不该带 error_message（前端据此判断走哪条渲染分支）。"""
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    result = next(e for e in events if e.type == "tool_result")
    assert result.data["ok"] is True
    assert result.data["error_message"] is None


# ---------------------------------------------------------------------------
# JSON 可序列化 —— Web 层的前提
# ---------------------------------------------------------------------------


async def test_all_event_data_is_json_serializable(
    settings: Settings, fake_weather, runner
) -> None:
    """这是硬约束：SSE 只能推 JSON，任何事件都不能有例外。"""
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    for event in events:
        json.dumps(event.to_dict(), ensure_ascii=False)  # 不抛异常即通过


async def test_plan_ready_contains_renderable_payload(
    settings: Settings, fake_weather, runner
) -> None:
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    ready = events[-1]
    assert ready.data["ok"] is True
    assert ready.data["step_count"] == 2
    assert ready.data["markdown"].startswith("## 计划")
    # 计划本身是纯 dict，前端可直接遍历渲染时间线
    assert isinstance(ready.data["plan"], dict)
    assert len(ready.data["plan"]["phases"]) == 2
    assert isinstance(ready.data["trace"], dict)
    assert ready.data["trace"]["stop_reason"] == "model_finished"


async def test_event_data_excludes_internal_messages(
    settings: Settings, fake_weather, runner
) -> None:
    """完整对话含系统提示词，体积大且不该外传，不能出现在事件里。"""
    orch = Orchestrator(_simple_script(), runner, settings)
    events = await _collect(orch, "成都 2 日游")

    assert "messages" not in events[-1].data
    for event in events:
        assert "messages" not in event.data


# ---------------------------------------------------------------------------
# 澄清中断：产出事件 → 挂起 → 回填
# ---------------------------------------------------------------------------


async def test_clarification_event_fires_and_blocks(settings: Settings, runner) -> None:
    """澄清必须产出独立事件，并把用户答案回灌给模型。"""
    asked: list[dict] = []
    resolved = asyncio.Event()

    async def async_ask(payload: dict) -> str:
        asked.append(payload)
        resolved.set()
        return "成都，10月3-4日，2 大 1 小"

    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(("c1", "ask_user_clarification", {"question": "去哪？几号？"})),
                plan_turn(make_plan()),
            ]
        ),
        runner,
        settings,
        ask_user=async_ask,
    )
    events = await _collect(orch, "我想去旅行")

    clar = [e for e in events if e.type == "clarification"]
    assert len(clar) == 1
    assert clar[0].data["question"] == "去哪？几号？"
    assert resolved.is_set()
    assert len(asked) == 1
    assert orch._last_result is not None
    assert orch._last_result.trace.interrupts == 1

    # 答案必须出现在回灌给模型的消息里
    tool_msgs = [m for m in orch._last_result.messages if m.get("role") == "tool"]
    assert any("成都" in m["content"] for m in tool_msgs)


async def test_sync_ask_user_still_supported(settings: Settings, runner) -> None:
    """向后兼容：CLI 的 input() 与既有测试用的是同步回调，不能破坏。"""

    def sync_ask(payload: dict) -> str:
        return "杭州"

    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(("c1", "ask_user_clarification", {"question": "去哪？"})),
                plan_turn(make_plan()),
            ]
        ),
        runner,
        settings,
        ask_user=sync_ask,
    )
    events = await _collect(orch, "我想去旅行")

    assert any(e.type == "clarification" for e in events)
    assert orch._last_result.trace.interrupts == 1


async def test_no_ask_user_degrades_without_crashing(settings: Settings, runner) -> None:
    """没有交互通道时应降级为「合理假设」，而不是抛异常。"""
    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(("c1", "ask_user_clarification", {"question": "去哪？"})),
                plan_turn(make_plan(assumptions=["默认成都"])),
            ]
        ),
        runner,
        settings,
        ask_user=None,
    )
    events = await _collect(orch, "我想去旅行")

    assert events[-1].data["ok"] is True
    tool_msgs = [m for m in orch._last_result.messages if m.get("role") == "tool"]
    assert any("没有交互通道" in m["content"] for m in tool_msgs)


# ---------------------------------------------------------------------------
# 入口一致性
# ---------------------------------------------------------------------------


async def test_run_async_and_run_stream_agree(settings: Settings, fake_weather, runner) -> None:
    """三条入口必须给出一致结果 —— 这是「重构行为不变」的核心保证。"""
    streamed = await _collect(
        Orchestrator(_simple_script(), runner, settings), "成都 2 日游"
    )
    via_async = await Orchestrator(_simple_script(), runner, settings).run_async("成都 2 日游")

    assert via_async.ok == streamed[-1].data["ok"]
    assert via_async.step_count == streamed[-1].data["step_count"]
    assert via_async.plan.goal == streamed[-1].data["plan"]["goal"]


def test_sync_run_matches_async(settings: Settings, fake_weather, runner) -> None:
    """同步入口 run() 是独立测试：它内部会 asyncio.run，不能在事件循环里调用。"""
    result = Orchestrator(_simple_script(), runner, settings).run("成都 2 日游")

    assert result.ok is True
    assert result.step_count == 2
    assert result.trace.stop_reason == "model_finished"


async def test_on_event_callback_receives_every_event(
    settings: Settings, fake_weather, runner
) -> None:
    seen: list[Event] = []
    orch = Orchestrator(_simple_script(), runner, settings)
    result = await orch.run_async("成都 2 日游", on_event=seen.append)

    assert result.ok is True
    assert [e.type for e in seen] == [
        "run_started",
        "turn_started",
        "tool_call",
        "tool_result",
        "turn_started",
        "plan_ready",
    ]


async def test_last_result_is_reset_between_runs(settings: Settings, fake_weather, runner) -> None:
    """同一实例连续跑两次，第二次不能读到第一次的残留结果。"""
    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)),
                plan_turn(make_plan(goal="第一次")),
                plan_turn(make_plan(goal="第二次")),
            ]
        ),
        runner,
        settings,
    )
    first = await orch.run_async("任务一")
    second = await orch.run_async("任务二")

    assert first.plan.goal == "第一次"
    assert second.plan.goal == "第二次"
    assert orch._last_result is second


# ---------------------------------------------------------------------------
# 修复重试也要有事件
# ---------------------------------------------------------------------------


async def test_repair_emits_event(settings: Settings, runner) -> None:
    orch = Orchestrator(
        ScriptedLLM([text_turn("没有 JSON"), plan_turn(make_plan())]),
        runner,
        settings,
    )
    events = await _collect(orch, "成都 2 日游")

    repairs = [e for e in events if e.type == "repair"]
    assert len(repairs) == 1
    assert repairs[0].data["reason"]
    assert events[-1].data["ok"] is True


async def test_budget_exhaustion_is_visible_in_events(
    settings: Settings, fake_weather, runner
) -> None:
    import dataclasses

    tight = dataclasses.replace(settings, tool_budget=1)
    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(
                    ("c1", "get_weather_forecast", WEATHER_ARGS),
                    ("c2", "query_attractions_db", {"city": "成都"}),
                ),
                plan_turn(make_plan()),
            ]
        ),
        runner,
        tight,
    )
    events = await _collect(orch, "成都 2 日游")

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 2
    assert results[0].data["ok"] is True
    assert results[1].data["ok"] is False
    assert results[1].data["budget_used"] == 1
    assert results[1].data["budget"] == 1
