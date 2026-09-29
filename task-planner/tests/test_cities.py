"""城市解析与国际城市支持测试。

对应两个真实缺陷：

1. **静默返回错误城市**：`get_weather_forecast(city="东京")` 曾命中
   Open-Meteo 地理编码里的「江苏·东京」(32.21,119.29)，
   不报错地把江苏天气当成东京天气返回 —— 比报错更糟。
2. **国际城市不可用**：景点库只有 5 个中国城市，`纽约` 查询直接 NOT_FOUND。

修复方式是引入本地城市坐标表（本地优先、API 回落 + 置信度标记），
并给景点库补上国际城市与币种字段。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_server.data.cities import (
    CITIES,
    cities_by_country,
    lookup_city,
    supported_city_names,
)
from mcp_server.tools.base import invoke, load_all_tools


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 城市表本身
# ---------------------------------------------------------------------------


def test_lookup_by_chinese_name() -> None:
    city = lookup_city("纽约")
    assert city is not None
    assert city.name_en == "New York"
    assert city.country == "美国"
    assert city.timezone == "America/New_York"
    assert city.lat == pytest.approx(40.7128, abs=0.01)


def test_lookup_by_english_name() -> None:
    assert lookup_city("New York").name_zh == "纽约"
    assert lookup_city("Tokyo").name_zh == "东京"
    assert lookup_city("Paris").name_zh == "巴黎"


def test_lookup_is_case_and_space_insensitive() -> None:
    assert lookup_city("new york").name_zh == "纽约"
    assert lookup_city("NEW YORK").name_zh == "纽约"
    assert lookup_city("newyork").name_zh == "纽约"
    assert lookup_city("  东京  ").name_en == "Tokyo"


def test_lookup_by_alias() -> None:
    assert lookup_city("NYC").name_zh == "纽约"
    assert lookup_city("三藩市").name_zh == "旧金山"
    assert lookup_city("西贡").name_zh == "胡志明市"
    assert lookup_city("登巴萨").name_zh == "巴厘岛"


def test_lookup_unknown_returns_none() -> None:
    assert lookup_city("霍格沃茨") is None
    assert lookup_city("") is None


def test_all_cities_have_valid_coordinates() -> None:
    for city in CITIES:
        assert -90 <= city.lat <= 90, city.name_zh
        assert -180 <= city.lon <= 180, city.name_zh
        assert city.timezone, city.name_zh
        assert city.country, city.name_zh


def test_city_names_are_unique() -> None:
    names = [c.name_zh for c in CITIES]
    assert len(names) == len(set(names)), "中文名有重复"


def test_hong_kong_is_labelled_as_china() -> None:
    """合规要求：香港标注为中国。"""
    assert lookup_city("香港").country == "中国"
    assert lookup_city("Hong Kong").country == "中国"


def test_covers_both_domestic_and_international() -> None:
    grouped = cities_by_country()
    assert "中国" in grouped
    assert len(grouped) >= 10, "应覆盖多个国家/地区"
    assert len(supported_city_names()) >= 50


# ---------------------------------------------------------------------------
# 天气：城市解析
# ---------------------------------------------------------------------------


def _patch_geocoding(monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]) -> dict[str, int]:
    """把地理编码换成固定响应，记录调用次数。"""
    import mcp_server.tools.weather as weather_mod

    counters = {"geocode": 0}

    def _fake(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if "geocoding" in url:
            counters["geocode"] += 1
            return {"results": results}
        return {
            "daily": {
                "time": ["2026-10-05"],
                "temperature_2m_max": [20.0],
                "temperature_2m_min": [10.0],
                "precipitation_probability_max": [10],
                "weathercode": [0],
            }
        }

    monkeypatch.setattr(weather_mod, "_fetch_json", _fake)
    return counters


def test_weather_resolves_international_city_from_local_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纽约必须走本地表解析，不依赖地理编码。"""
    counters = _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "纽约", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok, result.error
    resolved = result.data["resolved"]
    assert resolved["name"] == "纽约"
    assert resolved["country"] == "美国"
    assert resolved["matched_via"] == "local-city-table"
    assert resolved["confidence"] == "high"
    assert counters["geocode"] == 0, "本地表命中时不应调用地理编码"


def test_weather_tokyo_resolves_to_japan_not_jiangsu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：东京曾被解析成「江苏·东京」(32.21,119.29)。"""
    counters = _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "东京", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok
    resolved = result.data["resolved"]
    assert resolved["country"] == "日本"
    assert resolved["latitude"] == pytest.approx(35.6762, abs=0.01)
    assert resolved["longitude"] == pytest.approx(139.6503, abs=0.01)
    # 绝不能是江苏的坐标
    assert not (resolved["latitude"] == pytest.approx(32.21, abs=0.1))
    assert counters["geocode"] == 0


def test_weather_accepts_english_city_name(monkeypatch: pytest.MonkeyPatch) -> None:
    counters = _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "New York", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok
    assert result.data["resolved"]["name"] == "纽约"
    assert counters["geocode"] == 0


def test_weather_falls_back_to_geocoding_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本地表未收录的城市，回落地理编码并返回完整匹配元信息。"""
    counters = _patch_geocoding(
        monkeypatch,
        [
            {"latitude": 1.0, "longitude": 2.0, "name": "小地方", "feature_code": "PPL",
             "population": 500},
            {"latitude": 10.0, "longitude": 20.0, "name": "大城市", "country": "某国",
             "admin1": "某省", "feature_code": "PPLC", "population": 5_000_000,
             "timezone": "Asia/Shanghai"},
        ],
    )
    result = invoke(
        "get_weather_forecast",
        {"city": "某未知城市", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok
    assert counters["geocode"] == 1
    resolved = result.data["resolved"]
    # 必须按「行政级别 + 人口」挑最优候选，而不是拿第一个
    assert resolved["name"] == "大城市"
    assert resolved["matched_via"] == "open-meteo-geocoding"


def test_weather_flags_low_confidence_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """只匹配到低置信度结果时必须给出警告，而不是当成事实。"""
    _patch_geocoding(
        monkeypatch,
        [
            {"latitude": 32.21, "longitude": 119.29, "name": "东京", "country": "中国",
             "admin1": "江苏", "feature_code": "PPL", "population": 1200},
        ],
    )
    result = invoke(
        "get_weather_forecast",
        {"city": "某小城", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok
    assert result.data["resolved"]["confidence"] == "low"
    assert result.data["warnings"], "低置信度必须带警告"
    assert "低置信度" in result.data["warnings"][0]


def test_weather_unknown_city_lists_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "霍格沃茨", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert not result.ok
    assert result.error.code == "NOT_FOUND"
    assert "中国" in result.error.message
    assert "日本" in result.error.message


def test_weather_out_of_range_date_reports_allowed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：日期超出预报窗口时曾只报「400」，用户不知道该怎么办。"""
    import mcp_server.tools.weather as weather_mod

    request = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast")
    response = httpx.Response(
        400,
        json={
            "error": True,
            "reason": "Parameter 'start_date' is out of allowed range "
                      "from 2026-06-28 to 2026-10-14",
        },
        request=request,
    )

    def _boom(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(weather_mod, "_fetch_json", _boom)
    result = invoke(
        "get_weather_forecast",
        {"city": "纽约", "start_date": "2026-10-15", "end_date": "2026-10-16"},
    )
    assert not result.ok
    # 这是确定性的参数问题，不该被当作可重试的上游故障
    assert result.error.code == "BAD_ARGS"
    assert "2026-06-28" in result.error.message
    assert "2026-10-14" in result.error.message
    assert "非实时预报" in result.error.message


# ---------------------------------------------------------------------------
# 景点库：国际城市与币种
# ---------------------------------------------------------------------------


def test_attractions_supports_new_york_with_usd() -> None:
    result = invoke("query_attractions_db", {"city": "纽约", "limit": 10})
    assert result.ok, result.error
    assert result.data["country"] == "美国"
    assert result.data["currency"] == "USD"
    assert result.data["count"] >= 5
    for item in result.data["items"]:
        assert item["currency"] == "USD"
        assert item["country"] == "美国"


def test_attractions_totals_grouped_by_currency() -> None:
    result = invoke("query_attractions_db", {"city": "东京", "limit": 10})
    assert result.ok
    totals = result.data["totals_by_currency"]
    assert set(totals) == {"JPY"}
    assert totals["JPY"] > 0


def test_attractions_covers_twelve_cities() -> None:
    from mcp_server.data.seed import covered_cities

    cities = covered_cities()
    assert len(cities) == 12, cities
    for expected in ("纽约", "东京", "巴黎", "伦敦", "新加坡", "曼谷", "首尔"):
        assert expected in cities


def test_attractions_not_found_message_guides_the_model() -> None:
    result = invoke("query_attractions_db", {"city": "开罗"})
    assert not result.ok
    assert result.error.code == "NOT_FOUND"
    assert "不要凭记忆编造" in result.error.message


# ---------------------------------------------------------------------------
# 交通：国际城市
# ---------------------------------------------------------------------------


def test_route_accepts_english_names() -> None:
    result = invoke(
        "estimate_route", {"origin": "New York", "destination": "Tokyo", "mode": "flight"}
    )
    assert result.ok, result.error
    assert result.data["origin"] == "纽约"
    assert result.data["destination"] == "东京"


def test_route_international_distance_is_plausible() -> None:
    """纽约-伦敦直线距离约 5570 km。"""
    result = invoke(
        "estimate_route", {"origin": "纽约", "destination": "伦敦", "mode": "flight"}
    )
    assert result.ok
    assert 5200 < result.data["straight_line_km"] < 6000


def test_route_unknown_city_lists_supported() -> None:
    result = invoke("estimate_route", {"origin": "霍格沃茨", "destination": "纽约"})
    assert not result.ok
    assert result.error.code == "NOT_FOUND"
    assert "纽约" in result.error.message
