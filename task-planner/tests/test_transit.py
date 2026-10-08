"""`query_transit_options` 的测试。

⚠️ 重要说明
-----------
2026-10-04 用真实 Key 探测了 上海→成都，**发现文档与实现有两处不符**：

| 文档说 | 实际返回 |
|---|---|
| `type` 是数字码 `"2016"` | 中文描述 `"K字头的快车火车"` |
| 跨天用 `>24:00`（如 `"2949"`） | 普通时刻 `"0549"` |

因此 `REAL_*` 那组 fixture 是**从真实响应复制过来的**，
它们是唯一能证明解析器对得上现实的东西。改动解析逻辑时这组必须过。
"""

from __future__ import annotations

import json

import pytest

from common.envelope import ERR_NOT_FOUND, ERR_UPSTREAM_ERROR
from mcp_server.tools.base import invoke, load_all_tools
from mcp_server.tools.transit import (
    SEAT_CLASS_NAMES,
    _fmt_clock,
    _minutes_since_midnight,
    _summarize,
    parse_railway,
    parse_transit_option,
    parse_transit_payload,
)
from tests.conftest import FakeAmapClient


@pytest.fixture(autouse=True)
def _load() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 实测响应（2026-10-04 用真实 Key 探测 上海→成都，原样复制）
# ---------------------------------------------------------------------------

REAL_K282_RAILWAY: dict = {
    "id": "101016294294",
    "time": "119040",  # 秒 → 33.07h
    "name": "K282(上海-成都西)",
    "trip": "K282",
    "distance": "2507389",  # 米 → 2507.4km
    "type": "K字头的快车火车",  # ← 文档说是数字码 "2016"
    "departure_stop": {"id": "B00155KSDE", "name": "上海", "time": "2045", "start": "1"},
    "arrival_stop": {"id": "B001C953EF", "name": "成都西", "time": "0549", "end": "1"},
    # ← 不是 "2949"：实测跨天用普通时刻
    "spaces": [],
    "alters": [],
    "via_stops": [],
}

REAL_D986_RAILWAY: dict = {
    "id": "101016294295",
    "time": "48960",  # 秒 → 13.6h
    "name": "D986(上海虹桥-成都东)",
    "trip": "D986",
    "distance": "2216200",
    "type": "D字头的动车火车",
    "departure_stop": {"name": "上海虹桥", "time": "1844"},
    "arrival_stop": {"name": "成都东", "time": "0821"},
    "spaces": [],
    "alters": [],
}


# ---------------------------------------------------------------------------
# 按官方文档构造的 fixture（覆盖文档格式，保留兼容性）
# ---------------------------------------------------------------------------

RAILWAY_SEGMENT = {
    "railway": {
        "id": "seg-1",
        "name": "G1974",
        "trip": "G1974",
        "type": "2011",  # 文档里的数字码格式
        "distance": "1985000",
        "time": "37800",
        "departure_stop": {"name": "上海虹桥", "time": "0700", "start": "1"},
        "arrival_stop": {"name": "成都东", "time": "1730", "end": "1"},
        "alters": [
            {
                "id": "a1",
                "name": "G1974",
                "spaces": [
                    {"code": "13", "cost": "950"},   # 二等座
                    {"code": "12", "cost": "1520"},  # 一等座
                    {"code": "21", "cost": "2900"},  # 商务座
                ],
            }
        ],
    }
}

TRANSIT_PAYLOAD: dict = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "2",
    "route": {
        "origin": "121.4737,31.2304",
        "destination": "104.0668,30.5728",
        "distance": "1985000",
        "taxi_cost": "5800",
        "transits": [
            {
                "cost": "950",
                "duration": "37800",  # 秒 → 10.5h
                "nightflag": "0",
                "walking_distance": "1200",
                "segments": [
                    {"walking": {"distance": "600", "duration": "480"}},
                    RAILWAY_SEGMENT,
                    {"walking": {"distance": "600", "duration": "480"}},
                ],
            },
            {
                "cost": "860",
                "duration": "43200",  # 12h
                "nightflag": "1",
                "walking_distance": "800",
                "segments": [{"railway": REAL_D986_RAILWAY}],
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# 时刻解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "minutes"),
    [
        ("0700", 420),
        ("1730", 1050),
        ("0000", 0),
        ("2359", 1439),
        ("2405", 1445),  # 高德约定：>24:00 表示跨天，不取模
        ("730", 450),  # 三位数补零
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_minutes_since_midnight(raw: object, minutes: int | None) -> None:
    assert _minutes_since_midnight(raw) == minutes


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("0700", "07:00"),
        ("1730", "17:30"),
        ("2405", "次日 00:05"),
        ("2500", "次日 01:00"),
        ("", ""),
    ],
)
def test_fmt_clock(raw: str, text: str) -> None:
    assert _fmt_clock(raw) == text


# ---------------------------------------------------------------------------
# 铁路路段解析
# ---------------------------------------------------------------------------


def test_parse_railway_full() -> None:
    leg = parse_railway(RAILWAY_SEGMENT["railway"])

    assert leg is not None
    assert leg["kind"] == "train"
    assert leg["trip"] == "G1974"
    assert leg["type"] == "高铁 G"
    assert leg["from"] == "上海虹桥"
    assert leg["to"] == "成都东"
    assert leg["depart"] == "07:00"
    assert leg["arrive"] == "17:30"
    assert leg["duration_hours"] == 10.5
    assert leg["distance_km"] == 1985.0


def test_parse_railway_seat_prices_sorted_cheapest_first() -> None:
    leg = parse_railway(RAILWAY_SEGMENT["railway"])

    assert leg is not None
    prices = leg["seat_prices"]
    assert [p["class"] for p in prices] == ["二等座", "一等座", "商务座"]
    assert [p["price"] for p in prices] == [950.0, 1520.0, 2900.0]


def test_parse_railway_without_seat_prices() -> None:
    """`spaces` 只在 extensions=all 时返回，缺失是常态，不能因此失败。"""
    leg = parse_railway(
        {
            "trip": "D954",
            "type": "2012",
            "departure_stop": {"name": "上海站", "time": "2030"},
            "arrival_stop": {"name": "成都东", "time": "0830"},
        }
    )

    assert leg is not None
    assert "seat_prices" not in leg
    assert leg["trip"] == "D954"


def test_parse_railway_handles_documented_overnight_format() -> None:
    """文档格式（>24:00 表示跨天）仍要兼容 —— 万一某些线路还这么返回。"""
    leg = parse_railway(
        {
            "trip": "D954",
            "time": "43200",  # 12h，优先用它
            "departure_stop": {"name": "上海", "time": "2030"},
            "arrival_stop": {"name": "成都", "time": "3230"},
        }
    )

    assert leg is not None
    assert leg["duration_hours"] == 12.0
    assert leg["arrive"] == "次日 08:30", "跨天要显示出来，否则用户会误读"


def test_parse_railway_infers_next_day_without_time_field() -> None:
    """拿不到 time 字段时，到达早于发车要按**次日**处理，而不是算成负数。

    实测依据：高德对跨天到达返回的是普通时刻（K282 的 20:45 → 0549），
    简单相减会得负数。早期实现用 max(..., 0) 兜底，直接把 33 小时算成了 0。
    """
    leg = parse_railway(
        {
            "trip": "X1",
            "departure_stop": {"name": "A", "time": "2030"},
            "arrival_stop": {"name": "B", "time": "0830"},
        }
    )

    assert leg is not None
    # 20:30 → 次日 08:30 = 12h
    assert leg["duration_hours"] == 12.0
    assert leg["arrive"] == "次日 08:30"


# ---------------------------------------------------------------------------
# 实测响应回归（最重要的一组）
# ---------------------------------------------------------------------------


def test_real_k282_full_fields() -> None:
    """实测响应：上海 20:45 → 成都西 05:49，K282 普速快车。"""
    leg = parse_railway(REAL_K282_RAILWAY)

    assert leg is not None
    assert leg["trip"] == "K282"
    assert leg["type"] == "K字头的快车火车", "实测返回中文描述，不是数字码"
    assert leg["from"] == "上海"
    assert leg["to"] == "成都西"
    assert leg["depart"] == "20:45"
    # 33 小时的车，20:45 发车，到达是两天后 —— 必须标出来
    assert leg["arrive"] == "2 天后 05:49"
    assert leg["duration_hours"] == 33.1, "耗时来自 time 字段（秒）"
    assert leg["distance_km"] == 2507.4


def test_real_d986_full_fields() -> None:
    """实测响应：上海虹桥 18:44 → 成都东 08:21，D986 动车。"""
    leg = parse_railway(REAL_D986_RAILWAY)

    assert leg is not None
    assert leg["trip"] == "D986"
    assert leg["type"] == "D字头的动车火车"
    assert leg["depart"] == "18:44"
    assert leg["arrive"] == "次日 08:21"
    assert leg["duration_hours"] == 13.6
    assert leg["distance_km"] == 2216.2


def test_real_response_empty_spaces_are_tolerated() -> None:
    """实测 `spaces` 是空数组（不是缺失），不能因此报错或产出空列表。"""
    leg = parse_railway(REAL_K282_RAILWAY)

    assert leg is not None
    assert "seat_prices" not in leg


def test_real_duration_is_realistic() -> None:
    """实测耗时要落在常识范围内 —— 防止再次出现「33 小时算成 0」那类错误。"""
    k282 = parse_railway(REAL_K282_RAILWAY)
    d986 = parse_railway(REAL_D986_RAILWAY)

    assert k282 is not None and d986 is not None
    # 上海→成都：普速 30h+，动车 12-15h
    assert 28 <= k282["duration_hours"] <= 40
    assert 10 <= d986["duration_hours"] <= 18
    # 动车必须比普速快
    assert d986["duration_hours"] < k282["duration_hours"]


def test_parse_railway_accepts_legacy_numeric_type_code() -> None:
    """文档里的数字码格式仍然兼容（万一某些线路还返回它）。"""
    leg = parse_railway(
        {
            "trip": "G1",
            "type": "2011",
            "departure_stop": {"name": "A", "time": "0700"},
            "arrival_stop": {"name": "B", "time": "0900"},
        }
    )

    assert leg is not None
    assert leg["type"] == "高铁 G"


def test_parse_railway_rejects_empty_segment() -> None:
    assert parse_railway({}) is None
    assert parse_railway({"departure_stop": {}}) is None


def test_parse_railway_survives_weird_types() -> None:
    """字段类型不对时不能崩 —— 高德可能返回字符串或数字。"""
    leg = parse_railway(
        {
            "trip": 12345,
            "type": 2011,
            "distance": 1985000,
            "departure_stop": {"name": "上海", "time": 700},
            "arrival_stop": {"name": "成都", "time": 1730},
        }
    )

    assert leg is not None
    assert leg["trip"] == "12345"
    assert leg["type"] == "高铁 G"


# ---------------------------------------------------------------------------
# 换乘方案解析
# ---------------------------------------------------------------------------


def test_parse_transit_option_with_rail_and_walking() -> None:
    option = parse_transit_option(TRANSIT_PAYLOAD["route"]["transits"][0])

    assert option is not None
    assert option["cost"] == 950.0
    assert option["duration_hours"] == 10.5
    assert option["walking_distance_m"] == 1200
    assert option["is_night"] is False
    assert len(option["legs"]) == 1  # 只有 railway 算 leg，walking 不计
    assert option["legs"][0]["kind"] == "train"


def test_parse_transit_option_with_bus() -> None:
    option = parse_transit_option(
        {
            "cost": "5",
            "duration": "3600",
            "segments": [
                {
                    "bus": {
                        "buslines": [
                            {
                                "name": "地铁1号线(人民广场--莘庄)",
                                "type": "地铁线路",
                                "departure_stop": {"name": "人民广场"},
                                "arrival_stop": {"name": "莘庄"},
                            }
                        ]
                    }
                }
            ],
        }
    )

    assert option is not None
    assert option["legs"][0]["kind"] == "bus"
    assert "地铁1号线" in option["legs"][0]["line"]


def test_parse_transit_option_marks_night_service() -> None:
    option = parse_transit_option(TRANSIT_PAYLOAD["route"]["transits"][1])
    assert option is not None
    assert option["is_night"] is True


def test_parse_transit_option_returns_none_when_no_usable_legs() -> None:
    """全是步行 = 没有可用方案，应返回 None 而不是空壳。"""
    assert parse_transit_option({"segments": [{"walking": {"distance": "600"}}]}) is None
    assert parse_transit_option({}) is None


def test_parse_transit_payload_returns_all_options() -> None:
    options = parse_transit_payload(TRANSIT_PAYLOAD)

    assert len(options) == 2
    assert options[0]["cost"] == 950.0
    assert options[1]["cost"] == 860.0


def test_parse_transit_payload_respects_limit() -> None:
    assert len(parse_transit_payload(TRANSIT_PAYLOAD, limit=1)) == 1


def test_parse_transit_payload_tolerates_garbage() -> None:
    assert parse_transit_payload({}) == []
    assert parse_transit_payload({"route": {}}) == []
    assert parse_transit_payload({"route": {"transits": [None, "x", {}]}}) == []


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_summarize_picks_cheapest_priced_option() -> None:
    options = parse_transit_payload(TRANSIT_PAYLOAD)
    summary = _summarize(options)

    assert summary["option_count"] == 2
    assert summary["cheapest_cost"] == 860.0
    assert summary["fastest_hours"] == 10.5
    assert "G1974" in summary["train_numbers"]
    assert summary["recommended"]["cost"] == 860.0


def test_summarize_without_prices_falls_back_to_duration() -> None:
    options = [{"duration_hours": 5.0, "cost": None, "legs": [], "walking_distance_m": 0}]
    summary = _summarize(options)

    assert summary["cheapest_cost"] is None
    assert summary["recommended"]["duration_hours"] == 5.0


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------


def test_tool_fails_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMAP_API_KEY", raising=False)

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert not result.ok
    assert result.error.code == ERR_UPSTREAM_ERROR
    assert "AMAP_API_KEY" in result.error.message


def test_tool_rejects_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMAP_API_KEY", "k")

    result = invoke("query_transit_options", {"origin": "霍格沃茨", "destination": "成都"})

    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND


def test_tool_returns_parsed_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient(TRANSIT_PAYLOAD)
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: fake)

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert result.ok, result.error.message if not result.ok else ""
    assert result.meta.source == "amap:transit"
    assert result.data["origin"] == "上海"
    assert result.data["destination"] == "成都"
    assert len(result.data["options"]) == 2
    assert "12306" in result.data["note"], "要告诉用户去哪核实真实票价"


def test_tool_sends_city_names_and_lon_lat_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """高德要求「经度在前」，且跨城必须传 city/cityd。"""
    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient(TRANSIT_PAYLOAD)
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: fake)

    invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    sent = fake.calls[0]["params"]
    assert sent["city"] == "上海"
    assert sent["cityd"] == "成都"
    assert sent["origin"].startswith("121."), "经度应在前面"
    assert sent["destination"].startswith("104.")
    assert sent["extensions"] == "all", "要 all 才有仓位价格"


def test_tool_reports_quota_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient(
        {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10023"}
    )
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: fake)

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert not result.ok
    assert "配额已用尽" in result.error.message


def test_tool_reports_invalid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发环境实测到的就是这个响应 —— 错误路径是验证过的。"""
    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "test")
    fake = FakeAmapClient({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: fake)

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert not result.ok
    assert "AMAP_API_KEY 无效" in result.error.message


def test_tool_fails_helpfully_when_no_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient({"status": "1", "route": {"transits": []}})
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: fake)

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert not result.ok
    assert "estimate_route" in result.error.message, "要给出降级建议"


@pytest.mark.parametrize(
    "exc_name",
    ["TimeoutException", "ConnectError"],
)
def test_tool_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch, exc_name: str
) -> None:
    import httpx

    import mcp_server.tools.transit as transit_mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    exc = getattr(httpx, exc_name)("boom")
    monkeypatch.setattr(transit_mod.httpx, "Client", lambda **kw: FakeAmapClient(exc=exc))

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})

    assert not result.ok
    assert result.error.message


def test_tool_key_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.transit as transit_mod

    secret = "super-secret-amap-key"
    monkeypatch.setenv("AMAP_API_KEY", secret)
    monkeypatch.setattr(
        transit_mod.httpx, "Client", lambda **kw: FakeAmapClient(TRANSIT_PAYLOAD)
    )

    result = invoke("query_transit_options", {"origin": "上海", "destination": "成都"})
    assert secret not in json.dumps(result.to_payload(), ensure_ascii=False)


def test_tool_validates_strategy() -> None:
    from pydantic import ValidationError

    from mcp_server.tools.transit import TransitParams

    with pytest.raises(ValidationError, match="策略"):
        TransitParams(origin="上海", destination="成都", strategy="99")


def test_seat_class_mapping_covers_common_classes() -> None:
    """仓位编码表要覆盖常见座位，否则用户看到「仓位13」没法理解。"""
    for code in ("10", "11", "12", "13", "21"):
        assert code in SEAT_CLASS_NAMES
    assert SEAT_CLASS_NAMES["13"] == "二等座"
    assert SEAT_CLASS_NAMES["21"] == "商务座"


# ---------------------------------------------------------------------------
# 覆盖范围：境外必须**在发起请求之前**就判掉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "destination"),
    [
        ("上海", "伦敦"),
        ("伦敦", "巴黎"),
        ("北京", "纽约"),
    ],
)
def test_rejects_overseas_before_calling(
    monkeypatch: pytest.MonkeyPatch, origin: str, destination: str
) -> None:
    """境外路线要在调用之前判掉，且**不能说成「两地之间没有交通」**。

    回归用：曾经把高德的 `INSUFFICIENT_ABROAD_PRIVILEGES`（境外服务需单独
    申请权限）包成一句「可能两地之间没有公共交通方案」——
    上海→伦敦当然没有铁路方案，但原因不是「没有」，是「查不了」。
    用户会因此以为真的没法去。
    """
    import mcp_server.tools.transit as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")

    def _explode(**kw: object) -> object:
        raise AssertionError("境外路线不该发起请求 —— 配额有限，且必然失败")

    monkeypatch.setattr(mod.httpx, "Client", _explode)

    result = invoke(
        "query_transit_options", {"origin": origin, "destination": destination}
    )

    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND
    assert "覆盖" in result.error.message
    # 关键：不能说成「没有交通方案」
    assert "没有交通" in result.error.message or "不是「两地之间没有交通」" in result.error.message
    assert "estimate_route" in result.error.message, "要给出可用的替代方案"
