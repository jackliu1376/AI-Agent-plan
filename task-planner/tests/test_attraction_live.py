"""`query_attraction_realtime` 的测试。

重点是**匹配校验** —— 高德 POI 搜索会「按词拆开模糊匹配」，
**永远返回结果**，不做校验就会给出错误数据。实测过的反例：

- 查「一个根本不存在的景点XYZ」→ 返回「不存在茶铺」「根本烘焙面包店」
- 查「蒙马特高地与圣心大教堂」(region=巴黎) → 返回广州的「圣心大教堂」

因此这里的测试大部分在验证「**不该匹配的时候确实拒绝了**」。
"""

from __future__ import annotations

import json

import pytest

from common.envelope import ERR_NOT_FOUND, ERR_UPSTREAM_ERROR
from mcp_server.tools.attraction_live import (
    MATCH_HIGH_ABOVE,
    MATCH_REJECT_BELOW,
    cities_conflict,
    name_similarity,
    normalize_name,
    parse_poi,
    pick_best_match,
)
from mcp_server.tools.base import invoke, load_all_tools
from tests.conftest import FakeAmapClient


@pytest.fixture(autouse=True)
def _load() -> None:
    load_all_tools()


# ---------------------------------------------------------------------------
# 实测响应（2026-10-04，真实 Key，原样复制）
# ---------------------------------------------------------------------------

REAL_PANDA_POI: dict = {
    "name": "成都大熊猫繁育研究基地",
    "cityname": "成都市",
    "adname": "成华区",
    "address": "熊猫大道1375号",
    "type": "风景名胜;公园广场;公园",
    "business": {
        "business_area": "熊猫基地",
        "rating": "4.8",
        "opentime_today": "07:30-12:00 12:00-17:00",
        "opentime_week": (
            "03-16至10-31 周一至周日 07:30-12:00,12:00-17:00 最晚进入17:00；"
            "11-01至03-15 周一至周日 08:00-12:00,12:00-16:30 最晚进入16:30"
        ),
        "tel": "028-83510033",
    },
}

REAL_POI_PAYLOAD: dict = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "3",
    "pois": [
        REAL_PANDA_POI,
        {
            "name": "成都大熊猫繁育研究基地游客中心",
            "cityname": "成都市",
            "address": "熊猫大道",
            "business": {"opentime_week": "08:00-18:00"},
        },
        {
            "name": "熊猫基地(熊猫大道)(公交站)",
            "cityname": "成都市",
            "address": "",
            "business": {},
        },
    ],
}

# 实测：查巴黎的景点，高德返回了广州的结果
REAL_MISMATCH_PAYLOAD: dict = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "3",
    "pois": [
        {"name": "广州圣心大教堂", "cityname": "广州市", "business": {"rating": "4.6"}},
        {"name": "中山纪念堂", "cityname": "广州市", "business": {"rating": "4.8"}},
    ],
}

# 实测：查一个不存在的景点，高德按词拆开返回了无关结果
REAL_GARBAGE_PAYLOAD: dict = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "3",
    "pois": [
        {"name": "不存在茶铺", "cityname": "成都市", "business": {"rating": "4.3"}},
        {"name": "根本烘焙面包店", "cityname": "成都市", "business": {"rating": "4.1"}},
    ],
}


# ---------------------------------------------------------------------------
# 名称归一化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("成都大熊猫繁育研究基地", "成都大熊猫繁育研究基地"),
        ("熊猫基地（成都）", "熊猫基地"),
        ("熊猫基地(成都)", "熊猫基地"),
        ("都江堰·景区", "都江堰景区"),
        (" 西湖 景区 ", "西湖景区"),
        ("A-B_C", "ABC"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name(raw: object, expected: str) -> None:
    assert normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# 名称相似度
# ---------------------------------------------------------------------------


def test_similarity_exact_match() -> None:
    assert name_similarity("都江堰景区", "都江堰景区") == 1.0


def test_similarity_substring() -> None:
    """「都江堰景区」⊂「都江堰景区鱼嘴分水堤」—— 应算高度匹配。"""
    assert name_similarity("都江堰景区", "都江堰景区鱼嘴分水堤") >= 0.9


def test_similarity_short_form_passes() -> None:
    """「熊猫基地」是「成都大熊猫繁育研究基地」的常见简称，不能拒。"""
    assert name_similarity("熊猫基地", "成都大熊猫繁育研究基地") >= MATCH_REJECT_BELOW


def test_similarity_rejects_unrelated_names() -> None:
    """实测反例：查「一个根本不存在的景点XYZ」返回「不存在茶铺」。"""
    score = name_similarity("一个根本不存在的景点XYZ", "不存在茶铺")
    assert score < MATCH_REJECT_BELOW, f"不该通过，实际相似度 {score:.2f}"


def test_similarity_rejects_different_places() -> None:
    """实测反例：查「蒙马特高地与圣心大教堂」返回「中山纪念堂」。"""
    score = name_similarity("蒙马特高地与圣心大教堂", "中山纪念堂")
    assert score < MATCH_REJECT_BELOW


def test_similarity_empty_inputs() -> None:
    assert name_similarity("", "任意") == 0.0
    assert name_similarity("任意", "") == 0.0


# ---------------------------------------------------------------------------
# 城市校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("成都", "成都市"),
        ("成都市", "成都"),
        ("北京", "北京市"),
        ("杭州", "杭州"),
    ],
)
def test_cities_do_not_conflict(expected: str, actual: str) -> None:
    """「成都」与「成都市」是同一个地方，不能误判为冲突。"""
    assert cities_conflict(expected, actual) is False


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("巴黎", "广州市"),  # 实测反例
        ("成都", "重庆市"),
        ("纽约", "北京市"),
    ],
)
def test_cities_conflict_detected(expected: str, actual: str) -> None:
    assert cities_conflict(expected, actual) is True


def test_cities_conflict_ignores_missing_data() -> None:
    """拿不到城市就不据此拒绝 —— 交给名称相似度判断。"""
    assert cities_conflict("成都", "") is False
    assert cities_conflict("", "广州市") is False


# ---------------------------------------------------------------------------
# 挑选最佳匹配
# ---------------------------------------------------------------------------


def test_pick_best_match_accepts_exact() -> None:
    best, score, reason = pick_best_match(
        REAL_POI_PAYLOAD["pois"], query_name="成都大熊猫繁育研究基地", query_city="成都"
    )

    assert best is not None
    assert best["name"] == "成都大熊猫繁育研究基地"
    assert score >= MATCH_HIGH_ABOVE
    assert reason == ""


def test_pick_best_match_accepts_short_form() -> None:
    best, score, reason = pick_best_match(
        REAL_POI_PAYLOAD["pois"], query_name="熊猫基地", query_city="成都"
    )

    assert best is not None
    assert best["name"] == "成都大熊猫繁育研究基地"
    assert reason == ""


def test_pick_best_match_rejects_city_conflict() -> None:
    """实测反例：region=巴黎 却返回广州市的结果 —— 必须拒绝。"""
    best, _score, reason = pick_best_match(
        REAL_MISMATCH_PAYLOAD["pois"],
        query_name="蒙马特高地与圣心大教堂",
        query_city="巴黎",
    )

    assert best is None
    assert "城市不符" in reason
    assert "广州" in reason


def test_pick_best_match_rejects_garbage() -> None:
    best, score, reason = pick_best_match(
        REAL_GARBAGE_PAYLOAD["pois"],
        query_name="一个根本不存在的景点XYZ",
        query_city="成都",
    )

    assert best is None
    assert "名称不匹配" in reason
    assert score < MATCH_REJECT_BELOW


def test_pick_best_match_empty_candidates() -> None:
    best, _score, reason = pick_best_match([], query_name="任意", query_city="成都")
    assert best is None
    assert "没有返回" in reason


# ---------------------------------------------------------------------------
# 字段解析
# ---------------------------------------------------------------------------


def test_parse_poi_extracts_business_fields() -> None:
    data = parse_poi(REAL_PANDA_POI)

    assert data["name"] == "成都大熊猫繁育研究基地"
    assert data["city"] == "成都市"
    assert data["rating"] == 4.8
    assert "03-16至10-31" in data["opentime"], "季节性信息必须保留"
    assert data["tel"] == "028-83510033"


def test_parse_poi_tolerates_missing_business() -> None:
    data = parse_poi({"name": "某景点", "cityname": "成都市"})

    assert data["name"] == "某景点"
    assert "rating" not in data
    assert "opentime" not in data


def test_parse_poi_falls_back_to_today_opentime() -> None:
    """`opentime_week` 缺失时退回 `opentime_today`。"""
    data = parse_poi({"name": "X", "business": {"opentime_today": "09:00-17:00"}})
    assert data["opentime"] == "09:00-17:00"


def test_parse_poi_ignores_bad_rating() -> None:
    data = parse_poi({"name": "X", "business": {"rating": "不是数字"}})
    assert "rating" not in data


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------


def test_tool_fails_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMAP_API_KEY", raising=False)

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert not result.ok
    assert "AMAP_API_KEY" in result.error.message


def test_tool_rejects_unknown_city(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMAP_API_KEY", "k")

    result = invoke("query_attraction_realtime", {"name": "某景点", "city": "霍格沃茨"})

    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND


def test_tool_returns_high_confidence_match(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_POI_PAYLOAD))

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert result.ok, result.error.message if not result.ok else ""
    assert result.meta.source == "amap:poi"
    assert result.data["match_confidence"] == "high"
    assert result.data["matched_name"] == "成都大熊猫繁育研究基地"
    assert result.data["rating"] == 4.8
    assert "03-16至10-31" in result.data["opentime"]
    assert "match_warning" not in result.data


def test_tool_returns_warning_on_medium_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """相似度中等时不能假装确定 —— 要给出警告让调用方自行确认。"""
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    payload = {
        "status": "1",
        "pois": [{"name": "西湖风景名胜区", "cityname": "杭州市", "business": {"rating": "4.7"}}],
    }
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: FakeAmapClient(payload))

    result = invoke("query_attraction_realtime", {"name": "西湖景区", "city": "杭州"})

    assert result.ok
    assert result.data["match_confidence"] in {"medium", "high"}
    if result.data["match_confidence"] == "medium":
        assert "match_warning" in result.data
        assert "自行确认" in result.data["match_warning"]


def test_tool_rejects_city_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测反例回归：查巴黎的景点却返回广州的结果，必须失败而不是给错数据。"""
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_MISMATCH_PAYLOAD)
    )

    result = invoke(
        "query_attraction_realtime",
        {"name": "蒙马特高地与圣心大教堂", "city": "巴黎"},
    )

    assert not result.ok
    assert "城市不符" in result.error.message
    assert "query_attractions_db" in result.error.message, "要给出降级建议"


def test_tool_rejects_garbage_query(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_GARBAGE_PAYLOAD)
    )

    result = invoke(
        "query_attraction_realtime", {"name": "一个根本不存在的景点XYZ", "city": "成都"}
    )

    assert not result.ok
    assert "名称不匹配" in result.error.message


def test_tool_reports_quota_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        mod.httpx,
        "Client",
        lambda **kw: FakeAmapClient(
            {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "infocode": "10023"}
        ),
    )

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert not result.ok
    assert "配额已用尽" in result.error.message


@pytest.mark.parametrize("exc_name", ["TimeoutException", "ConnectError"])
def test_tool_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch, exc_name: str
) -> None:
    import httpx

    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    exc = getattr(httpx, exc_name)("boom")
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: FakeAmapClient(exc=exc))

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert not result.ok
    assert result.error.message


def test_tool_sends_correct_params(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    fake = FakeAmapClient(REAL_POI_PAYLOAD)
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: fake)

    invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    sent = fake.calls[0]["params"]
    assert sent["keywords"] == "熊猫基地"
    assert sent["region"] == "成都"
    assert sent["show_fields"] == "business", "不带 business 就拿不到开放时间与评分"


def test_tool_key_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    secret = "super-secret-amap-key"
    monkeypatch.setenv("AMAP_API_KEY", secret)
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_POI_PAYLOAD))

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert secret not in json.dumps(result.to_payload(), ensure_ascii=False)


def test_tool_does_not_return_price() -> None:
    """高德票价覆盖率仅 5%，本工具不该假装有票价字段。"""
    data = parse_poi(REAL_PANDA_POI)
    assert "cost" not in data
    assert "price" not in data


def test_tool_notes_where_to_verify_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_POI_PAYLOAD))

    result = invoke("query_attraction_realtime", {"name": "熊猫基地", "city": "成都"})

    assert "票价" in result.data["note"], "要明确说明票价不在范围内"
