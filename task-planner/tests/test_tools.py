"""工具层测试：8 个工具的行为、错误码与护栏。

对应需求文档中的 TC-05 / TC-06，以及全部「规则」章节的工程约束
（幂等、超时重试、沙箱、统一信封）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from common.envelope import (
    ERR_BAD_ARGS,
    ERR_FILE_ERROR,
    ERR_FORBIDDEN_PATH,
    ERR_NOT_FOUND,
    ERR_UPSTREAM_ERROR,
    ERR_UPSTREAM_TIMEOUT,
    ToolResult,
)
from mcp_server.tools.base import TOOL_REGISTRY, invoke, load_all_tools, openai_tool_schemas
from mcp_server.tools.route import CALIBRATION, summarize_route

EXPECTED_TOOLS = {
    "get_weather_forecast",
    "fetch_webpage",
    "query_attractions_db",
    "parse_budget_csv",
    "convert_currency",
    "estimate_route",
    "query_transit_options",
    "query_attraction_realtime",
    "save_itinerary",
    "ask_user_clarification",
}


@pytest.fixture(autouse=True)
def _load() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 注册表与 Schema
# ---------------------------------------------------------------------------


def test_registry_exposes_all_distinct_tools() -> None:
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


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (httpx.TimeoutException("timed out"), ERR_UPSTREAM_TIMEOUT),
        (httpx.ConnectError("connection refused"), ERR_UPSTREAM_ERROR),
    ],
)
def test_weather_network_errors_are_typed(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, expected_code: str
) -> None:
    """网络异常必须映射成**语义准确**的错误码，而不是笼统的 UNEXPECTED。

    回归用：曾经 ``resolve_city`` 里的地理编码调用完全没有 try，
    预报抓取也只 ``except httpx.HTTPStatusError`` —— 而
    ``httpx.TimeoutException`` / ``ConnectError`` 都不是它的子类，
    会一路冒泡到 ``invoke()`` 被归成 ``UNEXPECTED``。
    行为上仍可重试，但错误码说不清是「网络慢」还是「上游挂了」。
    """
    import mcp_server.tools.weather as weather_mod

    def _boom(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(weather_mod, "_fetch_json", _boom)

    # 「不存在的城市」逼它走地理编码兜底通道；成都走本地表、零网络
    result = invoke(
        "get_weather_forecast",
        {"city": "不存在的城市", "start_date": "2026-10-03", "end_date": "2026-10-04"},
        retries=0,
    )
    assert not result.ok
    assert result.error.code == expected_code
    assert "城市解析" in result.error.message, "消息应说明出错的是哪个阶段"


def test_weather_network_error_is_not_reported_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络故障不能被报成「查无此城」。

    这两者会导向完全不同的处理：NOT_FOUND 不可重试，模型会去换城市；
    UPSTREAM_* 可重试，模型应该稍后再试或如实说「暂时拿不到」。
    """
    import mcp_server.tools.weather as weather_mod

    def _boom(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(weather_mod, "_fetch_json", _boom)
    result = invoke(
        "get_weather_forecast",
        {"city": "不存在的城市", "start_date": "2026-10-03", "end_date": "2026-10-04"},
        retries=0,
    )
    assert result.error.code != ERR_NOT_FOUND
    assert result.error.code == ERR_UPSTREAM_ERROR


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
    """错误信息要说明覆盖范围，让模型知道能查什么。

    **刻意不列全部城市名**：扩到 250 城后，逐个列出会产出约 2000 字符的提示，
    每次失败都进模型上下文，纯属浪费 —— 而模型需要知道的只是「大致覆盖哪儿」。
    国家汇总就够了（看到「中国 195」就知道中国城市基本都有）。
    """
    result = invoke("query_attractions_db", {"city": "火星"})
    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND
    assert "覆盖" in result.error.message
    assert "中国" in result.error.message
    assert "不要凭记忆编造" in result.error.message
    # 防回归：这条信息会随每次失败进入模型上下文，不能膨胀回城市清单
    assert len(result.error.message) < 400, (
        f"提示过长（{len(result.error.message)} 字符），可能又列了全部城市"
    )


def test_attractions_distinguishes_missing_city_from_empty_filter() -> None:
    """「城市没收录」与「筛选太窄」必须给出**不同**的失败原因。

    混在一起说会误导模型：前者该换数据源，后者该放宽条件再查一次。
    （前车之鉴：高德的「覆盖不到」被写成「两地之间没有交通方案」，把用户带偏。）
    """
    missing = invoke("query_attractions_db", {"city": "火星"})
    assert not missing.ok
    assert "没有收录" in missing.error.message
    assert "筛空" not in missing.error.message

    too_narrow = invoke(
        "query_attractions_db", {"city": "成都", "tags": ["这个标签不存在"]}
    )
    assert not too_narrow.ok
    assert "筛空" in too_narrow.error.message
    assert "没有收录" not in too_narrow.error.message
    # 放宽条件是有用的建议，所以要把用到的条件回显出来
    assert "这个标签不存在" in too_narrow.error.message


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


def test_train_is_cheaper_and_slower_than_hsr() -> None:
    """普速必须**真的**按普速参数算，不能复用高铁分档。

    回归用：曾经 ``RAIL_MODES = {"hsr", "train"}`` 把两者都送进
    ``_estimate_rail()``，导致 ``MODE_PROFILE["train"]`` 成了死代码 ——
    普速按高铁算出来时间少一半、价格高一倍，而 ``mode_label``
    还写着「普速列车」。这条断言会立刻抓住那次回归。
    """
    args = {"origin": "上海", "destination": "成都"}
    hsr = invoke("estimate_route", {**args, "mode": "hsr"}).data
    train = invoke("estimate_route", {**args, "mode": "train"}).data

    assert train["duration_hours"] > hsr["duration_hours"], "普速应该比高铁慢"
    assert train["estimated_cost"] < hsr["estimated_cost"], "普速应该比高铁便宜"

    # 差距要足够大才算「两种方式」，而不是参数微调
    assert train["duration_hours"] > hsr["duration_hours"] * 1.5
    assert train["estimated_cost"] < hsr["estimated_cost"] * 0.7

    # 标签也要一致：摘要与 note 不能一个说「火车」一个说「普速列车」
    assert "普速" in train["note"]
    assert "普速" in summarize_route(train)
    assert "高铁" not in summarize_route(train)


def test_estimate_route_unknown_city() -> None:
    result = invoke("estimate_route", {"origin": "霍格沃茨", "destination": "成都"})
    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND


def test_route_error_guides_scenic_spot_handling() -> None:
    """景区级目的地无法解析时，错误信息必须给出可执行的降级路径。

    这是评测跑出来的真实缺口：模型会自然地想规划到「牛背山」「篁岭」这类
    景区（它们不在城市索引里，只有所属城市在）。
    错误信息若只说「无法解析」，模型只能摆烂或编造距离。

    **样本要选当前确实不在索引里的地名** —— 这条用例已经因为数据扩充
    换过两次（都江堰 → 青城山 → 牛背山）：每补一批景区级目的地，
    就会有一个旧样本变得可解析。改的时候挑一个还没收录的即可。
    """
    result = invoke("estimate_route", {"origin": "成都", "destination": "牛背山"})

    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND

    message = result.error.message
    assert "景区" in message, "要说明失败原因（不含景区/区县）"
    assert "最近的城市" in message, "要给出降级做法"
    assert "不要编造" in message, "要明确禁止编造"


# ---------------------------------------------------------------------------
# 高铁估算精度：用真实线路数据做回归
# ---------------------------------------------------------------------------
#
# 这组测试是「模型调参不许调坏」的守门人。参数在 mcp_server/tools/route.py
# 的 HSR_BANDS 里；改完跑这里就知道有没有退步。
#
# 注意口径：CALIBRATION 里的车程是**纯车程**（12306 显示的运行时长），
# 必须与输出里的 in_vehicle_hours 比，不能与 duration_hours（门到门）比 ——
# 早期就是因为混用这两个口径，得出了"误差 140%"的错误结论。


@pytest.mark.parametrize(
    ("origin", "dest", "straight", "rail_km", "hours", "fare"),
    CALIBRATION,
)
def test_route_distance_within_tolerance(
    origin: str, dest: str, straight: float, rail_km: float, hours: float, fare: float
) -> None:
    """铁路里程误差必须 ≤ 8%（当前实测 ≤4%）。"""
    data = invoke("estimate_route", {"origin": origin, "destination": dest, "mode": "hsr"}).data
    error = abs(data["distance_km"] / rail_km - 1)
    assert error <= 0.08, f"{origin}→{dest} 里程误差 {error:.1%}"


@pytest.mark.parametrize(
    ("origin", "dest", "straight", "rail_km", "hours", "fare"),
    CALIBRATION,
)
def test_route_duration_within_tolerance(
    origin: str, dest: str, straight: float, rail_km: float, hours: float, fare: float
) -> None:
    """车程点估计误差必须 ≤ 25%（当前实测 ≤22%）。"""
    data = invoke("estimate_route", {"origin": origin, "destination": dest, "mode": "hsr"}).data
    error = abs(data["in_vehicle_hours"] / hours - 1)
    assert error <= 0.25, f"{origin}→{dest} 车程误差 {error:.1%}"


@pytest.mark.parametrize(
    ("origin", "dest", "straight", "rail_km", "hours", "fare"),
    CALIBRATION,
)
def test_route_cost_within_tolerance(
    origin: str, dest: str, straight: float, rail_km: float, hours: float, fare: float
) -> None:
    """票价点估计误差必须 ≤ 18%（当前实测 ≤13%）。"""
    data = invoke("estimate_route", {"origin": origin, "destination": dest, "mode": "hsr"}).data
    error = abs(data["estimated_cost"] / fare - 1)
    assert error <= 0.18, f"{origin}→{dest} 费用误差 {error:.1%}"


@pytest.mark.parametrize(
    ("origin", "dest", "straight", "rail_km", "hours", "fare"),
    CALIBRATION,
)
def test_route_range_covers_actual(
    origin: str, dest: str, straight: float, rail_km: float, hours: float, fare: float
) -> None:
    """**最关键的一条**：真实值必须落在给出的区间内。

    点估计受线路标准影响做不到很准，但区间必须诚实 ——
    如果真实值落在区间外，说明区间给窄了，是在虚报精度。
    """
    data = invoke("estimate_route", {"origin": origin, "destination": dest, "mode": "hsr"}).data

    lo, hi = data["in_vehicle_range_hours"]
    assert lo <= hours <= hi, f"{origin}→{dest} 车程 {hours}h 不在区间 [{lo}, {hi}]"

    lo_c, hi_c = data["cost_range"]
    assert lo_c <= fare <= hi_c, f"{origin}→{dest} 票价 ¥{fare} 不在区间 [{lo_c}, {hi_c}]"


def test_route_duration_semantics_are_distinct() -> None:
    """门到门必须严格大于纯车程，差值等于进出站固定耗时。"""
    from mcp_server.tools.route import STATION_OVERHEAD_H

    data = invoke("estimate_route", {"origin": "北京", "destination": "上海", "mode": "hsr"}).data

    assert data["in_vehicle_hours"] < data["duration_hours"]
    assert data["duration_hours"] - data["in_vehicle_hours"] == pytest.approx(
        STATION_OVERHEAD_H, abs=0.15
    )


def test_route_ranges_are_ordered() -> None:
    """区间必须 low ≤ typical ≤ high，否则前端/模型会误读。"""
    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "hsr"}).data

    lo, hi = data["in_vehicle_range_hours"]
    assert lo <= data["in_vehicle_hours"] <= hi

    lo_c, hi_c = data["cost_range"]
    assert lo_c <= data["estimated_cost"] <= hi_c


def test_route_short_trip_is_not_overestimated() -> None:
    """短途曾因固定 overhead 被严重高估，这里钉住。"""
    data = invoke("estimate_route", {"origin": "上海", "destination": "杭州", "mode": "hsr"}).data
    # 实际最快 45 分钟，加上进出站约 1.4h；不该超过 2h
    assert data["in_vehicle_hours"] < 1.0
    assert data["duration_hours"] < 1.7


def test_route_reports_accuracy_and_assumption() -> None:
    """必须告诉调用方这是估算、精度多少、依据是什么。"""
    data = invoke("estimate_route", {"origin": "北京", "destination": "上海", "mode": "hsr"}).data

    assert "±" in data["accuracy"]
    assert "分档" in data["assumption"]
    assert "12306" in data["note"], "要告诉用户去哪查真实班次"


def test_route_detour_grows_with_distance() -> None:
    """短线线路更直，长线绕行更多 —— 分档系数的方向不能反。"""
    from mcp_server.tools.route import pick_rail_band

    assert pick_rail_band(150).detour < pick_rail_band(1000).detour
    assert pick_rail_band(150).detour <= 1.0  # 短线按直线走


def test_route_non_rail_modes_are_marked_uncalibrated() -> None:
    """驾车/大巴等没有真实数据标定，必须如实标注，不能伪装成同等精度。"""
    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"}).data

    assert "未用真实数据标定" in data["assumption"]
    assert "量级参考" in data["accuracy"]


def test_route_same_city_returns_zero() -> None:
    data = invoke("estimate_route", {"origin": "成都", "destination": "成都", "mode": "hsr"}).data
    assert data["distance_km"] == 0.0
    assert data["duration_hours"] == 0.0


# ---------------------------------------------------------------------------
# 驾车模式接高德（可选增强）
# ---------------------------------------------------------------------------
#
# 合规约束（见 route.py 顶部注释）：
# - 允许：接口文档的适用场景包含「无需展现地图的场景」
# - 禁止：服务协议禁止存储/缓存其数据 → 本项目不落库
# - 配额：个人 10000 次/月、2 QPS → 失败时降级而非重试


def test_drive_falls_back_without_key(no_amap_key: None) -> None:
    """未配置 key 时必须安静降级，行为与改造前一致。"""
    result = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"})

    assert result.ok
    assert result.meta.source == "offline:city-coords"
    assert "量级参考" in result.data["accuracy"]
    assert "未配置" not in result.data["note"], "没配 key 不该报错，只该静默降级"


def test_drive_uses_amap_when_key_present(fake_amap: dict[str, int]) -> None:
    """配置了 key 就走实时路线，source 要如实标注。"""
    result = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"})

    assert result.ok
    assert fake_amap["calls"] == 1
    assert result.meta.source == "amap:driving"
    assert result.data["distance_km"] == 1950.0
    assert "高德" in result.data["accuracy"]


def test_drive_amap_cost_splits_tolls_and_fuel(fake_amap: dict[str, int]) -> None:
    """费用必须拆成过路费（准确）+ 油费（估算），不能混成一个数。"""
    from mcp_server.tools.route import FUEL_COST_PER_KM

    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"}).data

    breakdown = data["cost_breakdown"]
    assert breakdown["tolls"] == 850.0
    assert breakdown["fuel"] == pytest.approx(1950.0 * FUEL_COST_PER_KM, abs=0.01)
    assert data["estimated_cost"] == pytest.approx(breakdown["tolls"] + breakdown["fuel"], abs=0.01)


def test_drive_amap_returns_ranges(fake_amap: dict[str, int]) -> None:
    """实时数据也要给区间 —— 路况会变，点估计会误导。"""
    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"}).data

    lo, hi = data["duration_range_hours"]
    assert lo <= data["duration_hours"] <= hi
    lo_c, hi_c = data["cost_range"]
    assert lo_c <= data["estimated_cost"] <= hi_c


def test_drive_degrades_when_amap_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """高德失败必须降级到离线估算，且如实说明原因 —— 不能静默假装成功。"""
    import mcp_server.tools.route as route_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        route_mod,
        "amap_driving",
        lambda *a, **kw: (None, "高德配额已用尽（个人账号 10000 次/月）"),
    )

    result = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"})

    assert result.ok, "降级后仍要给出估算，不能整个失败"
    assert result.meta.source == "offline:city-coords"
    assert "已降级为离线估算" in result.data["note"]
    assert "配额已用尽" in result.data["note"]


def test_drive_estimates_tolls_from_toll_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    """高德的 `tolls` 字段**实测恒为 0**，改用收费路段里程估算。

    2026-10-04 用真实 Key 验证了 4 条路线（含上海→成都 1883km 收费路段），
    `tolls` 全部返回 0；但 `toll_distance` 是准的。
    早期实现直接用 tolls，导致过路费永远是 0、总费用被严重低估。
    """
    import mcp_server.tools.route as route_mod
    from mcp_server.tools.route import TOLL_RATE_PER_KM

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        route_mod,
        "amap_driving",
        lambda *a, **kw: (
            {
                "distance_km": 1000.0,
                "duration_hours": 12.0,
                "tolls": 0.0,
                "toll_distance_km": 900.0,
            },
            "",
        ),
    )

    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"}).data

    assert data["cost_breakdown"]["tolls"] == pytest.approx(900.0 * TOLL_RATE_PER_KM, abs=0.01)
    assert "估算" in data["assumption"], "要说明过路费是估算的"
    assert data["estimated_cost"] > data["cost_breakdown"]["fuel"], "过路费不该被算成 0"


def test_drive_prefers_reported_tolls_when_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """万一高德将来开始返回真实过路费，要优先用它而不是继续估算。"""
    import mcp_server.tools.route as route_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        route_mod,
        "amap_driving",
        lambda *a, **kw: (
            {
                "distance_km": 1000.0,
                "duration_hours": 12.0,
                "tolls": 500.0,
                "toll_distance_km": 900.0,
            },
            "",
        ),
    )

    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"}).data

    assert data["cost_breakdown"]["tolls"] == 500.0
    assert "高德返回" in data["assumption"]


def test_amap_driving_parses_ok_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.route as route_mod
    from tests.conftest import AMAP_OK_PAYLOAD, FakeAmapClient

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient(AMAP_OK_PAYLOAD)
    monkeypatch.setattr(route_mod.httpx, "Client", lambda **kw: fake)

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert error == ""
    assert data is not None
    assert data["distance_km"] == 1950.0
    assert data["duration_hours"] == 20.0
    assert data["tolls"] == 850.0

    # 经纬度顺序必须是「经度在前」—— 高德的硬要求，写反会算到错误的地方
    sent = fake.calls[0]["params"]
    assert sent["origin"].startswith("121.47")
    assert sent["destination"].startswith("104.07")


def test_amap_driving_reports_quota_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.route as route_mod
    from tests.conftest import FakeAmapClient

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient({"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10023"})
    monkeypatch.setattr(route_mod.httpx, "Client", lambda **kw: fake)

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert data is None
    assert "配额已用尽" in error


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
        {"status": "1", "route": {"paths": []}},  # 无可用路线
        {"status": "1"},  # 缺 route
        {"status": "1", "route": {"paths": [{"distance": "abc"}]}},  # 字段异常
    ],
)
def test_amap_driving_never_raises(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """任何异常响应都必须返回错误信息而不是抛异常。"""
    import mcp_server.tools.route as route_mod
    from tests.conftest import FakeAmapClient

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(route_mod.httpx, "Client", lambda **kw: FakeAmapClient(payload))

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert data is None
    assert error, "必须给出可诊断的错误信息"


def test_amap_driving_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    import mcp_server.tools.route as route_mod
    from tests.conftest import FakeAmapClient

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        route_mod.httpx,
        "Client",
        lambda **kw: FakeAmapClient(exc=httpx.TimeoutException("timed out")),
    )

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert data is None
    assert "超时" in error


def test_amap_driving_handles_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.route as route_mod
    from tests.conftest import FakeAmapClient

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(route_mod.httpx, "Client", lambda **kw: FakeAmapClient("<html>502</html>"))

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert data is None
    assert "JSON" in error


def test_amap_driving_without_key_is_silent(no_amap_key: None) -> None:
    import mcp_server.tools.route as route_mod

    data, error = route_mod.amap_driving((121.47, 31.23), (104.07, 30.57))

    assert data is None
    assert "未配置" in error


def test_rail_mode_never_calls_amap(fake_amap: dict[str, int]) -> None:
    """高德的驾车接口对铁路无意义，不该被调用 —— 白烧配额。"""
    invoke("estimate_route", {"origin": "北京", "destination": "上海", "mode": "hsr"})

    assert fake_amap["calls"] == 0


def test_amap_key_does_not_leak_into_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """key 绝不能出现在返回给模型/前端的数据里。"""
    import mcp_server.tools.route as route_mod
    from tests.conftest import AMAP_OK_PAYLOAD, FakeAmapClient

    secret = "super-secret-amap-key"
    monkeypatch.setenv("AMAP_API_KEY", secret)
    monkeypatch.setattr(
        route_mod.httpx, "Client", lambda **kw: FakeAmapClient(AMAP_OK_PAYLOAD)
    )

    result = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": "drive"})
    blob = json.dumps(result.to_payload(), ensure_ascii=False)

    assert secret not in blob


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("hsr", "12306"),
        ("train", "12306"),
        ("drive", "地图"),
        ("flight", "航司"),
        ("bus", "地图"),
    ],
)
def test_route_channel_matches_mode(no_amap_key: None, mode: str, expected: str) -> None:
    """核实渠道必须与交通方式匹配 —— 给驾车用户提示「查 12306」毫无意义。"""
    data = invoke("estimate_route", {"origin": "上海", "destination": "成都", "mode": mode}).data

    assert expected in data["note"], f"{mode} 的核实渠道不对：{data['note']}"


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
