"""工具层测试：8 个工具的行为、错误码与护栏。

对应需求文档中的 TC-05 / TC-06，以及全部「规则」章节的工程约束
（幂等、超时重试、沙箱、统一信封）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.envelope import (
    ERR_BAD_ARGS,
    ERR_FILE_ERROR,
    ERR_FORBIDDEN_PATH,
    ERR_NOT_FOUND,
    ERR_UPSTREAM_TIMEOUT,
    ToolResult,
)
from mcp_server.tools.base import TOOL_REGISTRY, invoke, load_all_tools, openai_tool_schemas

EXPECTED_TOOLS = {
    "get_weather_forecast",
    "fetch_webpage",
    "query_attractions_db",
    "parse_budget_csv",
    "convert_currency",
    "estimate_route",
    "save_itinerary",
    "ask_user_clarification",
}


@pytest.fixture(autouse=True)
def _load() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 注册表与 Schema
# ---------------------------------------------------------------------------


def test_registry_exposes_eight_distinct_tools() -> None:
    """BYOA 要求 ≥2 个功能互异技能，这里实际提供 8 个。"""
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS
    assert len(TOOL_REGISTRY) >= 2


def test_openai_schemas_are_wellformed() -> None:
    schemas = openai_tool_schemas()
    assert len(schemas) == len(EXPECTED_TOOLS)
    for schema in schemas:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] in EXPECTED_TOOLS
        assert fn["description"].strip(), "每个工具都必须有 description"
        assert fn["parameters"]["type"] == "object"
        # 不允许残留 Pydantic 的 title 噪音
        assert "title" not in fn["parameters"]


# ---------------------------------------------------------------------------
# T1 天气
# ---------------------------------------------------------------------------


def test_weather_success(fake_weather: dict[str, int]) -> None:
    result = invoke(
        "get_weather_forecast",
        {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"},
    )
    assert result.ok, result.error
    assert result.data["city"] == "成都"
    assert len(result.data["daily"]) == 2
    assert result.data["daily"][0]["weather"] == "多云"  # WMO 2
    assert result.data["daily"][1]["weather"] == "小雨"  # WMO 61
    assert result.data["advice"]
    assert result.meta.source == "open-meteo.com"
    assert fake_weather["forecast"] == 1


def test_weather_rejects_bad_date_format() -> None:
    """确定性参数错误不应重试，直接返回 BAD_ARGS。"""
    result = invoke(
        "get_weather_forecast",
        {"city": "成都", "start_date": "2026/10/03", "end_date": "2026-10-04"},
    )
    assert not result.ok
    assert result.error.code == ERR_BAD_ARGS
    assert result.meta.attempts == 1


def test_weather_unknown_city_returns_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.weather as weather_mod

    monkeypatch.setattr(weather_mod, "_fetch_json", lambda url, params: {"results": []})
    result = invoke(
        "get_weather_forecast",
        {"city": "不存在的城市", "start_date": "2026-10-03", "end_date": "2026-10-04"},
    )
    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND


def test_weather_retries_once_then_fails(broken_weather: dict[str, int]) -> None:
    """TOOL_RETRIES=1 → 首次 + 重试共 2 次尝试，最终返回超时错误。"""
    result = invoke(
        "get_weather_forecast",
        {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"},
    )
    assert not result.ok
    assert result.error.code == ERR_UPSTREAM_TIMEOUT
    assert result.meta.attempts == 2
    assert broken_weather["attempts"] == 2


# ---------------------------------------------------------------------------
# T3 本地景点库（TC-05）
# ---------------------------------------------------------------------------


def test_attractions_filter_by_price_and_tag() -> None:
    """TC-05：成都、人均 ≤100、亲子友好。"""
    result = invoke(
        "query_attractions_db",
        {"city": "成都", "tags": ["亲子"], "max_price": 100, "limit": 10},
    )
    assert result.ok, result.error
    assert result.data["count"] >= 3
    for item in result.data["items"]:
        assert item["price"] <= 100
        assert "亲子" in item["tags"]


def test_attractions_unknown_city_lists_available() -> None:
    result = invoke("query_attractions_db", {"city": "火星"})
    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND
    assert "成都" in result.error.message  # 错误信息里给出可用城市


def test_attractions_is_idempotent() -> None:
    args = {"city": "杭州", "tags": ["自然"]}
    first = invoke("query_attractions_db", args)
    second = invoke("query_attractions_db", args)
    assert first.data == second.data


# ---------------------------------------------------------------------------
# T4 预算 CSV（TC-06）
# ---------------------------------------------------------------------------


def test_parse_budget_csv_totals_match_file() -> None:
    """TC-06：合计金额必须等于 CSV 各行之和。"""
    result = invoke(
        "parse_budget_csv",
        {"path": "mcp_server/data/sample_budget.csv", "total_budget": 3000},
    )
    assert result.ok, result.error
    data = result.data
    assert data["total"] == pytest.approx(sum(item["amount"] for item in data["line_items"]))
    assert data["total"] == pytest.approx(2930.0)
    assert data["by_category"]["交通"] == pytest.approx(1180.0)
    assert any("超支" in w for w in data["warnings"])  # 2930 <= 3000 → 未超支提示


def test_parse_budget_csv_detects_overspend() -> None:
    result = invoke(
        "parse_budget_csv",
        {"path": "mcp_server/data/sample_budget.csv", "total_budget": 2000},
    )
    assert result.ok
    assert any("超支" in w for w in result.data["warnings"])


def test_parse_budget_csv_blocks_path_traversal() -> None:
    result = invoke("parse_budget_csv", {"path": "../../../Windows/win.ini"})
    assert not result.ok
    assert result.error.code == ERR_FORBIDDEN_PATH


def test_parse_budget_csv_missing_file() -> None:
    result = invoke("parse_budget_csv", {"path": "mcp_server/data/not-here.csv"})
    assert not result.ok
    assert result.error.code == ERR_FILE_ERROR


# ---------------------------------------------------------------------------
# T5 汇率
# ---------------------------------------------------------------------------


def test_currency_offline_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络不可用时必须降级到离线汇率，并在 note 中显式标注。"""
    monkeypatch.setenv("EXCHANGE_RATE_API_URL", "")
    result = invoke(
        "convert_currency", {"amount": 100, "from_currency": "usd", "to_currency": "cny"}
    )
    assert result.ok, result.error
    assert result.data["from"] == "USD"  # 自动大写
    assert result.data["rate_source"] == "offline-fallback"
    assert "⚠️" in result.data["note"]
    assert result.data["result"] == pytest.approx(715.0)


def test_currency_identity() -> None:
    result = invoke(
        "convert_currency", {"amount": 50, "from_currency": "CNY", "to_currency": "CNY"}
    )
    assert result.ok
    assert result.data["rate"] == 1.0


def test_currency_rejects_bad_code() -> None:
    result = invoke(
        "convert_currency", {"amount": 1, "from_currency": "US", "to_currency": "CNY"}
    )
    assert not result.ok
    assert result.error.code == ERR_BAD_ARGS


# ---------------------------------------------------------------------------
# T6 交通估算（TC-09 的基础）
# ---------------------------------------------------------------------------


def test_estimate_route_shanghai_to_chengdu() -> None:
    result = invoke(
        "estimate_route", {"origin": "上海", "destination": "成都", "mode": "hsr"}
    )
    assert result.ok, result.error
    assert 1500 < result.data["straight_line_km"] < 2000  # 上海-成都直线约 1660km
    assert result.data["duration_hours"] > 0
    assert result.data["estimated_cost"] > 0


def test_estimate_route_is_symmetric() -> None:
    a = invoke("estimate_route", {"origin": "北京", "destination": "西安", "mode": "hsr"})
    b = invoke("estimate_route", {"origin": "西安", "destination": "北京", "mode": "hsr"})
    assert a.data["distance_km"] == b.data["distance_km"]


def test_estimate_route_unknown_city() -> None:
    result = invoke("estimate_route", {"origin": "霍格沃茨", "destination": "成都"})
    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND


def test_estimate_route_rejects_bad_mode() -> None:
    result = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "火箭"})
    assert not result.ok
    assert result.error.code == ERR_BAD_ARGS


# ---------------------------------------------------------------------------
# T7 落盘沙箱
# ---------------------------------------------------------------------------


def test_save_itinerary_inside_sandbox(sandbox: Path) -> None:
    result = invoke("save_itinerary", {"path": "trip.md", "content": "# 行程"})
    assert result.ok, result.error
    written = Path(result.data["path"])
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# 行程"
    assert sandbox in written.parents


def test_save_itinerary_blocks_traversal(sandbox: Path) -> None:
    result = invoke("save_itinerary", {"path": "../../evil.md", "content": "x"})
    assert not result.ok
    assert result.error.code == ERR_FORBIDDEN_PATH


def test_save_itinerary_creates_subdirectory(sandbox: Path) -> None:
    result = invoke("save_itinerary", {"path": "2026/chengdu.md", "content": "hi"})
    assert result.ok
    assert Path(result.data["path"]).parent.name == "2026"


# ---------------------------------------------------------------------------
# T8 澄清中断
# ---------------------------------------------------------------------------


def test_clarify_emits_interrupt_signal() -> None:
    result = invoke(
        "ask_user_clarification",
        {"question": "去哪里？", "options": ["成都", "杭州"]},
    )
    assert result.ok
    assert result.needs_user_input()
    assert result.meta.interrupt == "user_input"
    assert result.data["awaiting"] == "user_input"


def test_clarify_requires_question() -> None:
    result = invoke("ask_user_clarification", {"question": "   "})
    assert not result.ok
    assert result.error.code == ERR_BAD_ARGS


# ---------------------------------------------------------------------------
# 调用框架本身
# ---------------------------------------------------------------------------


def test_invoke_unknown_tool() -> None:
    result = invoke("no_such_tool", {})
    assert not result.ok
    assert result.error.code == ERR_BAD_ARGS
    assert "未知工具" in result.error.message


def test_invoke_always_returns_envelope(sandbox: Path) -> None:
    """无论成功失败，返回类型必须是 ToolResult。"""
    for name, args in [
        ("get_weather_forecast", {}),
        ("estimate_route", {"origin": "上海", "destination": "成都"}),
        ("save_itinerary", {"path": "x.md", "content": "y"}),
    ]:
        assert isinstance(invoke(name, args), ToolResult)


def test_envelope_payload_drops_none() -> None:
    payload = invoke("estimate_route", {"origin": "上海", "destination": "成都"}).to_payload()
    assert "error" not in payload  # 成功时 error 为 None，应被裁掉
