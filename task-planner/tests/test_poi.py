"""``agent/poi.py` 的测试。

这个模块只做两件事：从工具结果里**取坐标**，从计划正文里**判断景点属于哪天**。
两件事都是「宁可不出图，也不能画错」—— 地图上标错位置比没有地图更糟。

因此这里的测试重点是**拒绝**：
- 坐标非法 / 是 ``0,0`` 占位值 → 不收集
- 正文里找不到日次标记 → 全部归为未分组，而不是硬塞进 D1
"""

from __future__ import annotations

import json

from agent.poi import (
    PoiMarker,
    assign_days,
    collect_markers,
    extract_marker,
)
from mcp_server.tools.attraction_live import parse_location, parse_poi

# ---------------------------------------------------------------------------
# 坐标解析
# ---------------------------------------------------------------------------


def test_parse_location_normal() -> None:
    assert parse_location("120.153576,30.287459") == {"lon": 120.153576, "lat": 30.287459}


def test_parse_location_rejects_placeholder_zero() -> None:
    """高德对「没有坐标」填的是 ``0,0`` —— 不挡掉的话地图上会出现一个
    几内亚湾的点，用户会以为行程跑到非洲去了。"""
    assert parse_location("0,0") is None
    assert parse_location("0.0,0.0") is None


def test_parse_location_rejects_garbage() -> None:
    assert parse_location("") is None
    assert parse_location("120.15") is None
    assert parse_location("abc,def") is None
    assert parse_location("999,999") is None


def test_parse_poi_keeps_location() -> None:
    poi = {
        "name": "杭州西湖风景名胜区",
        "cityname": "杭州市",
        "address": "龙井路1号",
        "type": "风景名胜",
        "location": "120.153576,30.287459",
    }
    parsed = parse_poi(poi)
    assert parsed["location"] == {"lon": 120.153576, "lat": 30.287459}


def test_parse_poi_without_location_has_no_key() -> None:
    """没有坐标时**不写** ``location`` 键，而不是写 None ——
    下游用 ``in`` 判断，None 会让判断变复杂。"""
    parsed = parse_poi({"name": "某景点", "cityname": "杭州市"})
    assert "location" not in parsed


# ---------------------------------------------------------------------------
# 从工具结果提取标记
# ---------------------------------------------------------------------------


def _ok_payload(**overrides: object) -> dict:
    base = {
        "matched_name": "杭州西湖风景名胜区",
        "matched_city": "杭州市",
        "query": {"name": "西湖", "city": "杭州"},
        "location": {"lon": 120.153576, "lat": 30.287459},
    }
    base.update(overrides)
    return base


def test_extract_marker_ok() -> None:
    marker = extract_marker("query_attraction_realtime", _ok_payload())
    assert marker is not None
    assert marker.name == "杭州西湖风景名胜区"
    # query 与 name 都要留：前者用来在正文里找它属于哪天，后者用来显示
    assert marker.query == "西湖"
    assert marker.lon == 120.153576


def test_extract_marker_ignores_other_tools() -> None:
    """天气、路线工具也返回坐标，但那是城市中心，不是景点 —— 不能上地图。"""
    assert extract_marker("get_weather_forecast", _ok_payload()) is None


def test_extract_marker_rejects_missing_location() -> None:
    assert extract_marker("query_attraction_realtime", _ok_payload(location=None)) is None


def test_extract_marker_rejects_failed_result() -> None:
    assert extract_marker("query_attraction_realtime", None) is None


def test_extract_marker_rejects_blank_name() -> None:
    assert extract_marker("query_attraction_realtime", _ok_payload(matched_name="")) is None


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------


def _marker(name: str, lon: float = 120.0, lat: float = 30.0) -> PoiMarker:
    return PoiMarker(name=name, query=name, city="杭州", lon=lon, lat=lat)


def test_collect_dedupes_by_name() -> None:
    """修订会重跑工具，同一景点会被查两次 —— 不去重的话地图上会有两个
    完全重合的点，看起来像 bug。"""
    items: list[PoiMarker] = []
    items = collect_markers(items, _marker("西湖"))
    items = collect_markers(items, _marker("西湖"))
    assert len(items) == 1


def test_collect_ignores_none() -> None:
    items = [_marker("西湖")]
    assert collect_markers(items, None) is items


# ---------------------------------------------------------------------------
# 日次归属
# ---------------------------------------------------------------------------


def test_assign_days_markdown_headings() -> None:
    markdown = """
### D1 2026-11-07
上午去西湖，下午灵隐寺。

### D2 2026-11-08
西溪湿地一日。
"""
    markers = [_marker("西湖"), _marker("灵隐寺"), _marker("西溪湿地")]
    result = assign_days(markdown, markers)

    by_name = {m.name: m.day for m in result}
    assert by_name["西湖"] == "D1"
    assert by_name["灵隐寺"] == "D1"
    assert by_name["西溪湿地"] == "D2"


def test_assign_days_chinese_numeral() -> None:
    markdown = "第一天\n西湖\n\n第二天\n西溪湿地\n"
    markers = [_marker("西湖"), _marker("西溪湿地")]
    by_name = {m.name: m.day for m in assign_days(markdown, markers)}
    assert by_name["西湖"] == "D1"
    assert by_name["西溪湿地"] == "D2"


def test_assign_days_bold_day_label() -> None:
    markdown = "**D1** 西湖 · 灵隐寺\n**D2** 西溪湿地\n"
    markers = [_marker("西湖"), _marker("西溪湿地")]
    by_name = {m.name: m.day for m in assign_days(markdown, markers)}
    assert by_name["西湖"] == "D1"
    assert by_name["西溪湿地"] == "D2"


def test_assign_days_without_markers_keeps_all_ungrouped() -> None:
    """非旅游任务（写论文、做预算表）正文里不会有日次标记。
    这时所有点归为未分组 —— 不要硬造一个 D1。"""
    markdown = "第一步：确定选题。第二步：查文献。"
    markers = [_marker("西湖")]
    result = assign_days(markdown, markers)
    assert len(result) == 1
    assert result[0].day == ""


def test_assign_days_orders_within_day() -> None:
    markdown = "### D1\n先灵隐寺，再西湖。\n"
    markers = [_marker("西湖"), _marker("灵隐寺")]
    result = assign_days(markdown, markers)
    # 正文里灵隐寺在前，所以它应该排第一
    assert [m.name for m in result] == ["灵隐寺", "西湖"]
    assert [m.order for m in result] == [0, 1]


def test_assign_days_empty_inputs() -> None:
    assert assign_days("", []) == []
    assert assign_days("### D1\n西湖\n", []) == []
    result = assign_days("", [_marker("西湖")])
    assert len(result) == 1
    assert result[0].day == ""


def test_assign_days_matches_abbreviated_name() -> None:
    """模型查询时给全名，写正文时用简称 —— 这是实际跑出来的真实情况。

    查「雷峰塔景区」，正文写「苏堤 → 花港观鱼 → 雷峰塔」；
    查「南宋德寿宫遗址博物馆」，正文写「09:20 到德寿宫」。

    精确匹配会漏掉这一类，景点落到「未分组」里，地图上就没法按天筛选。
    """
    markdown = (
        "**D1（11-07 周六）西湖西线 + 灵隐**\n"
        "- 下午：苏堤 → 花港观鱼 → 雷峰塔（登塔看湖景）\n"
        "\n"
        "**D2（11-08 周日）老城文化 + 钱江夜景**\n"
        "- 09:20 到德寿宫，参观后步行至胡雪岩旧居\n"
    )
    markers = [
        PoiMarker(name="雷峰塔景区", query="雷峰塔景区", city="杭州市", lon=120.148849, lat=30.230934),
        PoiMarker(name="南宋德寿宫遗址博物馆", query="南宋德寿宫遗址博物馆", city="杭州市", lon=120.17, lat=30.24),
    ]

    by_name = {m.name: m.day for m in assign_days(markdown, markers)}
    assert by_name["雷峰塔景区"] == "D1"
    assert by_name["南宋德寿宫遗址博物馆"] == "D2"


def test_short_name_does_not_fall_back_to_partial_match() -> None:
    """两字名只认全名。

    「西湖」如果允许子串退化，会拿单个「西」或「湖」去匹配正文，
    而这两个字在任何一段杭州行程里都出现 —— 景点会被分到错误的那天。
    """
    from agent.poi import _locate

    assert _locate("杭州西湖风景名胜区", "西湖") == 2  # 完整命中
    assert _locate("杭州东站", "西湖") == -1  # 没有完整名字，不退化


def test_assign_days_prefers_most_frequent_day() -> None:
    """按「出现次数最多」判归属，而不是「第一次出现」。

    实测踩到：D1 段落里会顺带提到「明天去德寿宫」，而德寿宫真正属于 D2。
    按首次出现会把它分到 D1，地图上 D1 就多出一个不该有的点。
    """
    markdown = (
        "**D1 西湖西线**\n"
        "- 上午 断桥 → 白堤 → 雷峰塔\n"
        "- 说明：明天去德寿宫，今天先把西湖走完\n"
        "\n"
        "**D2 老城**\n"
        "- 09:20 到德寿宫，参观后去胡雪岩旧居\n"
        "- 下午回德寿宫附近吃饭\n"
    )
    markers = [
        PoiMarker(
            name="南宋德寿宫遗址博物馆", query="南宋德寿宫遗址博物馆",
            city="杭州市", lon=120.173273, lat=30.238678,
        )
    ]

    result = assign_days(markdown, markers)
    assert result[0].day == "D2"


def test_exact_match_beats_partial_match() -> None:
    """精确匹配优先，退化匹配只在前者全落空时启用。

    实测踩到：「南宋德寿宫遗址博物馆」在 D1 完整出现 1 次；D2 的标题写着
    「西湖南线 + 博物馆」、正文还有「杭州博物馆」，退化出的 3 字子串
    「博物馆」出现 2 次 —— 纯按次数投票会把归属判到 D2。
    """
    markdown = (
        "**D1 老城**\n"
        "- 傍晚：南宋德寿宫遗址博物馆（约 ¥30）\n"
        "\n"
        "**D2 西湖南线 + 博物馆**\n"
        "- 上午：雷峰塔景区\n"
        "- 下午：杭州博物馆\n"
    )
    markers = [
        PoiMarker(
            name="南宋德寿宫遗址博物馆", query="南宋德寿宫遗址博物馆",
            city="杭州市", lon=120.173273, lat=30.238678,
        )
    ]

    result = assign_days(markdown, markers)
    assert result[0].day == "D1"


def test_day_section_stops_at_next_chapter() -> None:
    """日次段落在下一个二级标题处结束，不能一路吃到文档末尾。

    否则最后一个 D 段落会把「计划表」「预算」「风险」全算进去 ——
    那些地方提到景点的次数往往更多，归属就判错了。
    """
    from agent.poi import _day_sections

    markdown = (
        "## 逐日路线\n"
        "**D1 西湖**\n- 断桥、白堤\n"
        "**D2 西溪**\n- 西溪湿地\n"
        "## 计划表\n"
        "| S1 | 订票 |\n"
    )
    sections = _day_sections(markdown)
    d2 = next(s for s in sections if s[0] == "D2")
    assert "计划表" not in markdown[d2[1] : d2[2]]


# ---------------------------------------------------------------------------
# 事件通道
# ---------------------------------------------------------------------------


def test_event_data_carries_pois() -> None:
    """坐标走 ``plan_ready`` 事件下发，而不是进 ``Plan`` schema。

    进 schema 的话模型就必须输出坐标 —— 它不知道真实值，只会编造。
    """
    from agent.loop import LoopTrace, PlanResult

    marker = PoiMarker(
        name="杭州西湖风景名胜区", query="西湖", city="杭州市",
        lon=120.153576, lat=30.287459, day="D1", order=0,
    )
    result = PlanResult(
        ok=True, plan=None, markdown="### D1\n西湖", raw_text="", trace=LoopTrace(),
        pois=[marker],
    )

    data = result.to_event_data()

    # 事件 data 必须 JSON 可序列化 —— Web 层直接原样推给前端
    json.dumps(data)
    assert data["pois"] == [
        {
            "name": "杭州西湖风景名胜区",
            "query": "西湖",
            "city": "杭州市",
            "lon": 120.153576,
            "lat": 30.287459,
            "day": "D1",
            "order": 0,
        }
    ]
