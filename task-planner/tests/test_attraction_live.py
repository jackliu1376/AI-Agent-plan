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
    nearby,
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


def test_nearby_accepts_county_level_poi() -> None:
    """坐标复核能救回被城市名误杀的县级市 POI。

    高德的 ``cityname`` 对县级市填的是**上级行政区**：查「稻城」返回
    「甘孜藏族自治州」，查「康定」同样。字符串比对判为冲突，
    会把**完全正确**的数据拒掉 —— 实测用户查「稻城亚丁」时，
    高德明明返回了「甘孜稻城亚丁景区」（adcode 513337 正是稻城县），
    却被城市冲突检查拦下。
    """
    poi = {"name": "甘孜稻城亚丁景区", "cityname": "甘孜藏族自治州",
           "location": "100.2977,29.0378"}  # 稻城县城坐标

    assert cities_conflict("稻城", poi["cityname"]) is True, "字符串层面确实对不上"
    assert nearby("稻城", poi) is True, "但坐标对得上，不该拒绝"

    # 反向：真的错配（巴黎查询返回广州）坐标差得远，仍然要拒绝
    far = {"name": "广州塔", "cityname": "广州市", "location": "113.3245,23.1065"}
    assert nearby("巴黎", far) is False


def test_pick_best_match_accepts_county_level_via_coordinates() -> None:
    """整合验证：cityname 是州名但坐标正确时，应该采纳。"""
    pois = [{
        "name": "甘孜稻城亚丁景区", "cityname": "甘孜藏族自治州",
        "location": "100.2977,29.0378", "type": "风景名胜",
    }]
    best, score, reason = pick_best_match(
        pois, query_name="稻城亚丁", query_city="稻城"
    )

    assert best is not None, f"被误杀了：{reason}"
    assert best["name"] == "甘孜稻城亚丁景区"
    assert score >= MATCH_HIGH_ABOVE



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
    """实测反例回归：查一个城市的景点却返回另一个城市的结果，必须失败而不是给错数据。

    **注意查询城市必须是覆盖范围内的。** 用「巴黎」的话会在发起请求**之前**
    就被 `amap_covers` 拦下（见 test_tool_rejects_overseas_before_calling），
    走不到 `pick_best_match` 的城市校验 —— 这条测试就失去意义了。
    """
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")
    monkeypatch.setattr(
        mod.httpx, "Client", lambda **kw: FakeAmapClient(REAL_MISMATCH_PAYLOAD)
    )

    result = invoke(
        "query_attraction_realtime",
        {"name": "蒙马特高地与圣心大教堂", "city": "成都"},
    )

    assert not result.ok
    assert "城市不符" in result.error.message
    assert "query_attractions_db" in result.error.message, "要给出降级建议"


# ---------------------------------------------------------------------------
# 覆盖范围：境外必须**在发起请求之前**就判掉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "city"),
    [
        ("大英博物馆", "伦敦"),
        ("埃菲尔铁塔", "巴黎"),
        ("自由女神像", "纽约"),
        ("台北101", "台北"),  # 实测高德对台湾也返回空，别想当然算进覆盖范围
    ],
)
def test_tool_rejects_overseas_before_calling(
    monkeypatch: pytest.MonkeyPatch, name: str, city: str
) -> None:
    """境外城市要在**调用之前**判掉，不能靠解析返回值来发现。

    回归用：曾经只有「城市能否解析」这一道检查，而伦敦 / 巴黎都能解析
    （在 1.4 万城市索引里），于是照样去查高德 ——
    高德对不认识的 `region` **不报错，而是静默降级成全国关键词模糊搜索**：

    · 查「大英博物馆」→ 遂宁市大英县的「大英汉陶博物馆」
    · 查「伦敦塔桥」→ 北京世界公园里的微缩景观

    结果是白烧一次配额，最后抛一句让人摸不着头脑的「城市不符」。
    """
    import mcp_server.tools.attraction_live as mod

    monkeypatch.setenv("AMAP_API_KEY", "k")

    def _explode(**kw: object) -> object:
        raise AssertionError("境外城市不该发起请求 —— 配额有限，且结果必然是错的")

    monkeypatch.setattr(mod.httpx, "Client", _explode)

    result = invoke("query_attraction_realtime", {"name": name, "city": city})

    assert not result.ok
    assert result.error.code == ERR_NOT_FOUND
    assert "覆盖" in result.error.message
    # 必须说清「这不是景点不存在」，否则模型会据此下错误结论
    assert "不是这个景点不存在" in result.error.message
    assert "query_attractions_db" in result.error.message, "要给出可用的替代方案"


@pytest.mark.parametrize("city", ["成都", "香港", "澳门"])
def test_amap_covers_domestic_and_hk_mo(city: str) -> None:
    """港澳必须算在覆盖范围内 —— 实测高德对它们有数据（香港 86 条、澳门 96 条）。

    把港澳误判成境外会白白丢掉本来能拿到的实时数据。
    """
    from mcp_server.data.cities import lookup_city
    from mcp_server.tools.route import amap_covers

    info = lookup_city(city)
    assert info is not None, city
    assert amap_covers(info), f"{city}（{info.country}）不该被拦"


@pytest.mark.parametrize("city", ["伦敦", "巴黎", "纽约", "台北"])
def test_amap_does_not_cover_overseas(city: str) -> None:
    """境外（含台湾）不在覆盖范围内。

    **台湾特别容易想当然**：实测 `台北101` 高德返回空，
    所以 `中国台湾` 不能算进覆盖范围。
    """
    from mcp_server.data.cities import lookup_city
    from mcp_server.tools.route import amap_covers

    info = lookup_city(city)
    assert info is not None, city
    assert not amap_covers(info), f"{city}（{info.country}）应该被拦"


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
