"""城市解析测试 —— 三层策略（策展表 / GeoNames 索引 / 在线兜底）。

背景
----
早期版本完全依赖 Open-Meteo 地理编码解析城市名，实测**未收录城市的失败率约 40%**，
而且最危险的是"静默返回错误城市"：

    name=开罗    -> 开罗/美国 (37.0,-89.2)      ← 美国伊利诺伊州的 Cairo
    name=里斯本   -> 里斯本/美国 (46.4,-97.7)    ← 美国北达科他州的 Lisbon
    name=米兰    -> 米兰/美国
    name=伊斯坦布尔 / 华沙 / 内罗毕 / 加德满都 -> 无结果

现在改为本地优先：策展表（170 条，含非城市目的地）→ GeoNames 生成索引
（14,359 个中文城市名）→ 在线地理编码（带低置信度标记）。
本文件把当时出错的城市全部钉成回归用例。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_server.data.cities import (
    CURATED,
    cities_by_country,
    index_size,
    lookup_city,
    supported_city_names,
)
from mcp_server.data.countries import COUNTRY_ZH, country_zh
from mcp_server.tools.base import invoke, load_all_tools


@pytest.fixture(autouse=True)
def _load_tools() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 回归：早期解析错误 / 查不到的城市
# ---------------------------------------------------------------------------

# (查询词, 期望国家)  —— 全部来自实测失败清单
REGRESSION_CITIES: list[tuple[str, str]] = [
    ("开罗", "埃及"),          # 曾误解析为美国伊利诺伊州
    ("里斯本", "葡萄牙"),       # 曾误解析为美国北达科他州
    ("米兰", "意大利"),         # 曾误解析为美国
    ("马尼拉", "菲律宾"),       # 曾误解析为美国
    ("伊斯坦布尔", "土耳其"),    # 曾无结果
    ("华沙", "波兰"),           # 曾无结果
    ("雷克雅未克", "冰岛"),      # 曾无结果
    ("内罗毕", "肯尼亚"),       # 曾无结果
    ("加德满都", "尼泊尔"),      # 曾无结果
    ("基辅", "乌克兰"),         # 曾无结果
    ("利马", "秘鲁"),           # 曾无结果
    ("科伦坡", "斯里兰卡"),      # GeoNames 只有台译「可倫坡」
    ("圣托里尼", "希腊"),       # 岛屿，不在城市数据集里
    ("维也纳", "奥地利"),       # GeoNames 只有繁体「維也納」
    ("日内瓦", "瑞士"),         # 曾误解析为机场
    ("开普敦", "南非"),         # 曾误解析为机场
    ("布拉格", "捷克"),
    ("雅典", "希腊"),
    ("慕尼黑", "德国"),
    ("威尼斯", "意大利"),
    ("哥本哈根", "丹麦"),
]


@pytest.mark.parametrize(("query", "country"), REGRESSION_CITIES)
def test_regression_cities_resolve_to_correct_country(query: str, country: str) -> None:
    city = lookup_city(query)
    assert city is not None, f"{query} 应该能解析"
    assert city.country == country, f"{query} 应属 {country}，实际 {city.country}"
    assert -90 <= city.lat <= 90 and -180 <= city.lon <= 180
    assert city.timezone


def test_cairo_is_not_illinois() -> None:
    """最危险的那个 case：开罗曾命中美国伊利诺伊州的 Cairo。"""
    city = lookup_city("开罗")
    assert city is not None
    assert city.country == "埃及"
    assert city.lat == pytest.approx(30.04, abs=0.5)   # 开罗在 30°N
    assert city.lon == pytest.approx(31.24, abs=0.5)
    assert not (35 < city.lat < 40), "不应落在美国纬度"


def test_lisbon_is_not_north_dakota() -> None:
    city = lookup_city("里斯本")
    assert city is not None
    assert city.country == "葡萄牙"
    assert city.lon == pytest.approx(-9.14, abs=0.5)   # 里斯本在西经 9°


# ---------------------------------------------------------------------------
# 索引覆盖（GeoNames 生成，1.4 万条）
# ---------------------------------------------------------------------------


def test_index_is_loaded_and_substantial() -> None:
    assert index_size() > 10_000, f"索引过小：{index_size()}"


@pytest.mark.parametrize(
    ("query", "country"),
    [
        ("特罗姆瑟", "挪威"),
        ("琅勃拉邦", "老挝"),
        ("杜布罗夫尼克", "克罗地亚"),
        ("萨尔茨堡", "奥地利"),
        ("克拉科夫", "波兰"),
        ("塔林", "爱沙尼亚"),
        ("里加", "拉脱维亚"),
        ("地拉那", "阿尔巴尼亚"),
        ("第比利斯", "格鲁吉亚"),
        ("巴库", "阿塞拜疆"),
        ("阿拉木图", "哈萨克斯坦"),
        ("乌兰巴托", "蒙古"),
        ("顺化", "越南"),
        ("会安", "越南"),
        ("日惹", "印度尼西亚"),
        ("仰光", "缅甸"),
        ("斋浦尔", "印度"),
        ("安曼", "约旦"),
        ("吉达", "沙特阿拉伯"),
        ("波尔多", "法国"),
    ],
)
def test_index_covers_less_common_cities(query: str, country: str) -> None:
    city = lookup_city(query)
    assert city is not None, f"{query} 应被索引收录"
    assert city.country == country, f"{query} 应属 {country}，实际 {city.country}"
    assert city.source == "city-index"


# ---------------------------------------------------------------------------
# 归一化：行政区后缀 / 繁简 / 大小写 / 别名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("达沃市", "达沃"),      # 索引里是「达沃市」，用户可能只写「达沃」
        ("达沃", "达沃"),
        ("科威特城", "科威特城"),
        ("上海市", "上海"),
        ("北京市", "北京"),
    ],
)
def test_administrative_suffix_is_tolerated(variant: str, expected: str) -> None:
    city = lookup_city(variant)
    assert city is not None, f"{variant} 应能解析"
    assert city.name_zh == expected


def test_suffix_stripping_does_not_create_junk_keys() -> None:
    """「盐城」不能因为剥掉「城」而变成无意义的「盐」。"""
    city = lookup_city("盐城")
    assert city is not None
    assert city.country == "中国"
    assert city.name_zh in {"盐城", "盐城市"}


@pytest.mark.parametrize(
    ("traditional", "simplified"),
    [
        ("維也納", "维也纳"),
        ("米蘭", "米兰"),
        ("開羅", "开罗"),
        ("華沙", "华沙"),
        ("伊斯坦堡", "伊斯坦布尔"),
        ("烏布", "乌布"),
    ],
)
def test_traditional_chinese_input_works(traditional: str, simplified: str) -> None:
    city = lookup_city(traditional)
    assert city is not None, f"繁体输入 {traditional} 应能解析"
    assert city.name_zh == simplified


def test_case_and_spacing_insensitive() -> None:
    assert lookup_city("new york").name_zh == "纽约"
    assert lookup_city("NEW YORK").name_zh == "纽约"
    assert lookup_city("newyork").name_zh == "纽约"
    assert lookup_city("  东京  ").name_en == "Tokyo"


def test_alias_lookup() -> None:
    assert lookup_city("NYC").name_zh == "纽约"
    assert lookup_city("三藩市").name_zh == "旧金山"
    assert lookup_city("西贡").name_zh == "胡志明市"
    assert lookup_city("登巴萨").name_zh == "巴厘岛"
    assert lookup_city("乔治市").name_zh == "槟城"
    assert lookup_city("可倫坡").name_zh == "科伦坡"


def test_unknown_city_returns_none() -> None:
    assert lookup_city("霍格沃茨") is None
    assert lookup_city("") is None
    assert lookup_city("   ") is None


# ---------------------------------------------------------------------------
# 非城市目的地（策展表专门兜住的部分）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "country"),
    [
        ("圣托里尼", "希腊"),
        ("长滩岛", "菲律宾"),
        ("薄荷岛", "菲律宾"),
        ("苏梅岛", "泰国"),
        ("富国岛", "越南"),
        ("龙目岛", "印度尼西亚"),
        ("美娜多", "印度尼西亚"),
        ("济州岛", "韩国"),
        ("冲绳", "日本"),
        ("关岛", "美国"),
        ("塞班", "美国"),
        ("大溪地", "法属波利尼西亚"),
        ("米科诺斯", "希腊"),
        ("五渔村", "意大利"),
        ("少女峰", "瑞士"),
        ("哈尔施塔特", "奥地利"),
        ("班夫", "加拿大"),
        ("黄石国家公园", "美国"),
        ("大峡谷", "美国"),
        ("优胜美地", "美国"),
        ("尼亚加拉瀑布", "加拿大"),
        ("马丘比丘", "秘鲁"),
        ("复活节岛", "智利"),
        ("乌尤尼", "玻利维亚"),
        ("佩特拉", "约旦"),
        ("死海", "约旦"),
        ("卢克索", "埃及"),
        ("桑给巴尔", "坦桑尼亚"),
        ("塞伦盖蒂", "坦桑尼亚"),
        ("维多利亚瀑布", "津巴布韦"),
        ("卡帕多奇亚", "土耳其"),
        ("棉花堡", "土耳其"),
    ],
)
def test_non_city_destinations_are_covered(query: str, country: str) -> None:
    """岛屿 / 景区 / 国家公园这类目的地，城市数据集通常不收，必须策展兜住。"""
    city = lookup_city(query)
    assert city is not None, f"{query} 应被策展表收录"
    assert city.country == country
    assert city.source == "curated-table"


# ---------------------------------------------------------------------------
# 策展表本身
# ---------------------------------------------------------------------------


def test_curated_cities_have_valid_coordinates() -> None:
    for city in CURATED:
        assert -90 <= city.lat <= 90, city.name_zh
        assert -180 <= city.lon <= 180, city.name_zh
        assert city.timezone, city.name_zh
        assert city.country, city.name_zh


def test_curated_chinese_names_are_unique() -> None:
    names = [c.name_zh for c in CURATED]
    assert len(names) == len(set(names)), "策展表中文名重复"


def test_curated_covers_many_countries() -> None:
    grouped = cities_by_country()
    assert len(grouped) >= 40, f"策展表只覆盖 {len(grouped)} 个国家/地区"
    assert len(supported_city_names()) >= 100


def test_hong_kong_macau_taiwan_labelled_as_china() -> None:
    """合规要求：香港 / 澳门 / 台湾均为中国的一部分。"""
    assert lookup_city("香港").country == "中国"
    assert lookup_city("Hong Kong").country == "中国"
    assert lookup_city("澳门").country == "中国"
    assert lookup_city("台北").country == "中国台湾"
    assert lookup_city("高雄").country == "中国台湾"


def test_country_code_mapping_is_compliant() -> None:
    assert COUNTRY_ZH["HK"] == "中国香港"
    assert COUNTRY_ZH["MO"] == "中国澳门"
    assert COUNTRY_ZH["TW"] == "中国台湾"
    assert COUNTRY_ZH["CN"] == "中国"


def test_country_zh_falls_back_to_code() -> None:
    assert country_zh("eg") == "埃及"
    assert country_zh("ZZ") == "ZZ"   # 未收录不编造


# ---------------------------------------------------------------------------
# 天气工具集成
# ---------------------------------------------------------------------------


def _patch_geocoding(
    monkeypatch: pytest.MonkeyPatch, results: list[dict[str, Any]]
) -> dict[str, int]:
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


def test_weather_uses_curated_table_without_geocoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counters = _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "纽约", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok, result.error
    resolved = result.data["resolved"]
    assert resolved["name"] == "纽约"
    assert resolved["country"] == "美国"
    assert resolved["matched_via"] == "curated-table"
    assert counters["geocode"] == 0


def test_weather_uses_city_index_for_small_cities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counters = _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "特罗姆瑟", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert result.ok, result.error
    assert result.data["resolved"]["matched_via"] == "city-index"
    assert result.data["resolved"]["country"] == "挪威"
    assert counters["geocode"] == 0, "索引命中时不应发起地理编码请求"


def test_weather_falls_back_to_geocoding_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert resolved["name"] == "大城市", "必须按行政级别+人口挑最优候选，而不是取第一条"
    assert resolved["matched_via"] == "open-meteo-geocoding"


def test_weather_flags_low_confidence_match(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_weather_unknown_city_reports_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_geocoding(monkeypatch, [])
    result = invoke(
        "get_weather_forecast",
        {"city": "霍格沃茨", "start_date": "2026-10-05", "end_date": "2026-10-05"},
    )
    assert not result.ok
    assert result.error.code == "NOT_FOUND"
    assert "本地城市库已收录" in result.error.message
    assert "不要凭记忆编造" in result.error.message


def test_weather_out_of_range_date_reports_allowed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert result.error.code == "BAD_ARGS", "确定性参数问题不该被当成可重试的上游故障"
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
    assert set(result.data["totals_by_currency"]) == {"JPY"}


def test_attractions_covers_all_provincial_capitals() -> None:
    """景点库必须覆盖中国全部 34 个省级行政区。

    4 直辖市 + 23 省省会 + 5 自治区首府 + 2 特别行政区。
    """
    from mcp_server.data.seed import covered_cities

    cities = set(covered_cities())

    municipalities = {"北京", "天津", "上海", "重庆"}
    provincial_capitals = {
        "石家庄", "太原", "沈阳", "长春", "哈尔滨", "南京", "杭州", "合肥",
        "福州", "南昌", "济南", "郑州", "武汉", "长沙", "广州", "海口",
        "成都", "贵阳", "昆明", "西安", "兰州", "西宁", "台北",
    }
    autonomous_capitals = {"呼和浩特", "南宁", "拉萨", "银川", "乌鲁木齐"}
    sar = {"香港", "澳门"}

    expected = municipalities | provincial_capitals | autonomous_capitals | sar
    assert len(expected) == 34, f"省级行政区数量应为 34，实际 {len(expected)}"

    missing = expected - cities
    assert not missing, f"景点库缺少这些省级行政区: {sorted(missing)}"


def test_attractions_also_covers_international_cities() -> None:
    from mcp_server.data.seed import covered_cities

    cities = set(covered_cities())
    for expected in ("纽约", "东京", "巴黎", "伦敦", "新加坡", "曼谷", "首尔"):
        assert expected in cities


def test_attractions_dataset_size() -> None:
    from mcp_server.data.seed import SEED_ROWS, covered_cities

    assert len(covered_cities()) >= 42
    assert len(SEED_ROWS) >= 280
    # 每个城市至少 5 个景点，否则推荐质量太差
    from collections import Counter

    per_city = Counter(row[0] for row in SEED_ROWS)
    thin = {city: n for city, n in per_city.items() if n < 5}
    assert not thin, f"以下城市景点过少: {thin}"


def test_every_provincial_capital_has_kid_friendly_options() -> None:
    """亲子游是常见场景，每个省会都应有可带小孩去的地方。"""
    from mcp_server.data.seed import SEED_ROWS

    capitals = {
        "北京", "天津", "上海", "重庆", "石家庄", "太原", "呼和浩特", "沈阳",
        "长春", "哈尔滨", "南京", "杭州", "合肥", "福州", "南昌", "济南",
        "郑州", "武汉", "长沙", "广州", "南宁", "海口", "成都", "贵阳",
        "昆明", "拉萨", "西安", "兰州", "西宁", "银川", "乌鲁木齐",
    }
    kid_friendly: set[str] = set()
    for row in SEED_ROWS:
        if row[8] == 1:  # kid_friendly 字段
            kid_friendly.add(row[0])

    missing = capitals - kid_friendly
    assert not missing, f"以下省会没有亲子友好景点: {sorted(missing)}"


def test_attractions_hong_kong_macau_taipei_use_local_currency() -> None:
    """港澳台用当地货币计价，并标注为中国的组成部分。"""
    hk = invoke("query_attractions_db", {"city": "香港", "limit": 3})
    assert hk.ok and hk.data["country"] == "中国" and hk.data["currency"] == "HKD"

    mo = invoke("query_attractions_db", {"city": "澳门", "limit": 3})
    assert mo.ok and mo.data["country"] == "中国" and mo.data["currency"] == "MOP"

    tw = invoke("query_attractions_db", {"city": "台北", "limit": 3})
    assert tw.ok and tw.data["country"] == "中国台湾" and tw.data["currency"] == "TWD"


def test_mop_currency_can_be_converted(monkeypatch: pytest.MonkeyPatch) -> None:
    """澳门元必须能换算，否则澳门行程的预算算不出来。"""
    monkeypatch.setenv("EXCHANGE_RATE_API_URL", "")
    result = invoke(
        "convert_currency", {"amount": 100, "from_currency": "MOP", "to_currency": "CNY"}
    )
    assert result.ok, result.error
    assert result.data["rate_source"] == "offline-fallback"
    assert result.data["result"] == pytest.approx(88.9)


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


def test_route_works_for_index_only_cities() -> None:
    """只靠索引解析的城市，交通估算也要能用（坐标直接取自解析结果）。"""
    result = invoke(
        "estimate_route", {"origin": "特罗姆瑟", "destination": "奥斯陆", "mode": "flight"}
    )
    assert result.ok, result.error
    assert result.data["origin"] == "特罗姆瑟"
    assert 900 < result.data["straight_line_km"] < 1300


def test_route_international_distance_is_plausible() -> None:
    """纽约-伦敦直线距离约 5570 km。"""
    result = invoke(
        "estimate_route", {"origin": "纽约", "destination": "伦敦", "mode": "flight"}
    )
    assert result.ok
    assert 5200 < result.data["straight_line_km"] < 6000


def test_route_unknown_city_reports_coverage() -> None:
    result = invoke("estimate_route", {"origin": "霍格沃茨", "destination": "纽约"})
    assert not result.ok
    assert result.error.code == "NOT_FOUND"
    assert "本地城市库已收录" in result.error.message
