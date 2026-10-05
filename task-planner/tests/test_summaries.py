"""工具人话摘要（`ToolSpec.summarize`）的测试。

背景
----
前端要把「Agent 在查什么、查到了什么」翻译成人话展示，
而不是把 `get_weather_forecast 1464ms · 预算 1/12` 这种内部实现丢给用户。

摘要由**每个工具自己**生成 —— 只有工具知道返回数据里哪些字段是重点。
本文件锁住三件事：

1. 摘要**绝不抛异常**（它只是展示层的东西，不能把成功的调用变成失败）；
2. 每个工具的摘要措辞符合预期；
3. `tool_result` 事件真的带上了摘要（前端靠它渲染）。
"""

from __future__ import annotations

import pytest

from agent.config import Settings
from agent.demo import ScriptedLLM
from agent.loop import Orchestrator
from mcp_server.tools.base import TOOL_REGISTRY, load_all_tools
from tests.conftest import make_plan, plan_turn, tool_turn

load_all_tools()

WEATHER_ARGS = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}

# 有摘要的工具：这些工具的返回值是「查证到的事实」，值得展示
TOOLS_WITH_SUMMARY = {
    "get_weather_forecast",
    "query_attractions_db",
    "query_attraction_realtime",
    "estimate_route",
    "query_transit_options",
    "convert_currency",
    "fetch_webpage",
}


# ---------------------------------------------------------------------------
# 契约：绝不抛异常
# ---------------------------------------------------------------------------


def test_summary_of_returns_empty_without_summarizer() -> None:
    """没配摘要的工具返回空串，而不是报错。"""
    spec = TOOL_REGISTRY["save_itinerary"]
    assert spec.summarize is None
    assert spec.summary_of({"path": "a.md"}) == ""


def test_summary_of_returns_empty_for_non_dict() -> None:
    spec = TOOL_REGISTRY["get_weather_forecast"]
    assert spec.summary_of(None) == ""
    assert spec.summary_of("字符串") == ""
    assert spec.summary_of([1, 2]) == ""


def test_summary_of_swallows_exceptions() -> None:
    """摘要炸了不能影响主流程 —— 它只是锦上添花。

    这里直接构造一个必然抛异常的 summarize，验证兜底。
    """
    from mcp_server.tools.base import ToolSpec

    def boom(_data: dict) -> str:
        raise RuntimeError("故意炸")

    spec = ToolSpec(
        name="x",
        description="",
        params_model=TOOL_REGISTRY["get_weather_forecast"].params_model,
        func=lambda p: None,  # type: ignore[arg-type,return-value]
        summarize=boom,
    )
    assert spec.summary_of({"a": 1}) == ""


def test_all_summarizers_handle_empty_payload() -> None:
    """每个工具的摘要都必须能吃空字典 —— 上游字段缺失是常态。"""
    for name in TOOLS_WITH_SUMMARY:
        assert TOOL_REGISTRY[name].summary_of({}) == ""


# ---------------------------------------------------------------------------
# 各工具的措辞
# ---------------------------------------------------------------------------


def test_weather_summary_mentions_range_and_rain() -> None:
    summary = TOOL_REGISTRY["get_weather_forecast"].summary_of(
        {
            "daily": [
                {"date": "2026-10-03", "tmax": 26, "tmin": 18, "precip_prob": 20, "weather": "多云"},
                {"date": "2026-10-04", "tmax": 24, "tmin": 17, "precip_prob": 70, "weather": "小雨"},
            ]
        }
    )

    assert "10-03 起 2 天" in summary
    assert "多云" in summary
    assert "17–26°C" in summary
    assert "10-04 有小雨" in summary, "降水概率高的日子要点出来，这是用户最关心的"


def test_weather_summary_omits_rain_when_dry() -> None:
    summary = TOOL_REGISTRY["get_weather_forecast"].summary_of(
        {"daily": [{"date": "2026-10-03", "tmax": 26, "tmin": 18, "precip_prob": 5, "weather": "晴"}]}
    )
    assert "小雨" not in summary


def test_attractions_summary_shows_price_range() -> None:
    summary = TOOL_REGISTRY["query_attractions_db"].summary_of(
        {"currency": "CNY", "items": [{"price": 0}, {"price": 180}, {"price": 55}]}
    )
    assert summary == "匹配到 3 个景点，人均 ¥0–180"


def test_attractions_summary_single_price_not_a_range() -> None:
    summary = TOOL_REGISTRY["query_attractions_db"].summary_of(
        {"currency": "CNY", "items": [{"price": 55}, {"price": 55}]}
    )
    assert summary == "匹配到 2 个景点，人均 ¥55"


def test_route_summary_uses_in_vehicle_hours() -> None:
    """必须用纯车程（in_vehicle_hours），不是门到门（duration_hours）。

    这里只给 duration_hours，摘要应当忽略它 —— 否则用户看到的耗时
    会和班次时刻表对不上。
    """
    summary = TOOL_REGISTRY["estimate_route"].summary_of(
        {
            "origin": "上海",
            "destination": "成都",
            "mode": "hsr",
            "in_vehicle_hours": 8.2,
            "duration_hours": 8.9,
            "estimated_cost": 917,
        }
    )
    assert summary == "上海 → 成都，高铁 8.2h / ¥917"


def test_currency_summary() -> None:
    summary = TOOL_REGISTRY["convert_currency"].summary_of(
        {"amount": 1000, "from": "CNY", "to": "USD", "result": 140.2}
    )
    assert summary == "1000 CNY = 140.2 USD"


def test_attraction_live_summary_truncates_opentime() -> None:
    """开放时间原文很长（含季节性/闭馆日），摘要里要截断 —— 完整内容在计划正文。"""
    summary = TOOL_REGISTRY["query_attraction_realtime"].summary_of(
        {
            "matched_name": "成都大熊猫繁育研究基地",
            "rating": "4.7",
            "opentime": "03-16 至 10-31 07:30-18:00，11-01 起冬季时段，周一闭馆",
        }
    )
    assert summary.startswith("成都大熊猫繁育研究基地")
    assert "评分 4.7" in summary
    assert summary.endswith("…"), "超长部分要截断，否则一行放不下"


def test_transit_summary() -> None:
    summary = TOOL_REGISTRY["query_transit_options"].summary_of(
        {
            "origin": "上海",
            "destination": "成都",
            "summary": {"fastest_hours": 8.2, "cheapest_cost": 917, "option_count": 6},
        }
    )
    assert summary == "上海 → 成都，最快 8.2h，最低 ¥917，6 个方案"


def test_web_summary_falls_back_to_host_when_title_missing() -> None:
    summary = TOOL_REGISTRY["fetch_webpage"].summary_of(
        {"url": "https://example.com/a", "title": "(无标题)", "total_chars": 3200}
    )
    assert summary == "example.com（3,200 字）"


# ---------------------------------------------------------------------------
# 集成：事件里真的带上了摘要
# ---------------------------------------------------------------------------


async def test_tool_result_event_carries_summary(
    settings: Settings, fake_weather, runner
) -> None:
    """前端靠 `tool_result.data.summary` 渲染「已查证」列表，必须真的在。"""
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)),
            plan_turn(make_plan()),
        ]
    )
    orch = Orchestrator(llm, runner, settings)

    events = [event async for event in orch.run_stream("成都 2 日游")]
    result = next(e for e in events if e.type == "tool_result")

    assert "summary" in result.data
    assert isinstance(result.data["summary"], str)
    assert len(result.data["summary"]) > 0, "天气工具应当给出摘要"


async def test_failed_tool_has_empty_summary(settings, runner) -> None:
    """失败的调用没有摘要（没有数据可描述），前端会显示错误码。"""
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "estimate_route", {"origin": "霍格沃茨", "destination": "成都"})),
            plan_turn(make_plan()),
        ]
    )
    orch = Orchestrator(llm, runner, settings)

    events = [event async for event in orch.run_stream("测试")]
    result = next(e for e in events if e.type == "tool_result")

    assert result.data["ok"] is False
    assert result.data["summary"] == ""


async def test_event_payload_still_json_serializable(
    settings: Settings, fake_weather, runner
) -> None:
    """Web 层要把事件原样推给前端，加了 summary 也不能破坏这个约束。"""
    import json

    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER_ARGS)),
            plan_turn(make_plan()),
        ]
    )
    orch = Orchestrator(llm, runner, settings)

    async for event in orch.run_stream("成都 2 日游"):
        json.dumps({"type": event.type, "data": event.data}, ensure_ascii=False)


@pytest.mark.parametrize("name", sorted(TOOLS_WITH_SUMMARY))
def test_summarizer_is_wired_up(name: str) -> None:
    assert TOOL_REGISTRY[name].summarize is not None, f"{name} 忘了接 summarize"
