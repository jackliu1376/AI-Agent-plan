"""工具 T10：query_attraction_realtime —— 实时核对景点开放时间与评分（高德 POI）。

为什么需要它
------------
``query_attractions_db`` 用的是静态种子库，**反映不了开放时间变更**
（季节调整、周一闭馆、节假日例外）。高德 POI 的 ``opentime_week``
实测是结构化的，且含这些信息：

    故宫：旺季4月1日至10月31日 周二至周日 08:30-17:00 16:00停止检票，
          淡季11月1日至3月31日…周一全天关闭，节假日营业时间以官方通知为准

2026-10-04 实测 20 个随机景点，覆盖率：

======================  ========  ==========================================
字段                     覆盖率     说明
======================  ========  ==========================================
``rating``               89%       评分
``opentime_week``        74%       开放时间（含季节性/闭馆日）
``tel``                  58%       电话
``cost``                 **5%**    ⚠️ 票价基本拿不到
======================  ========  ==========================================

**票价仍是空白** —— 高德给不出（5%），大众点评有但仅 B 端合作，
Google Places 无此字段，Wikidata 无此属性。所以本工具**不假装有票价**。

⚠️ 匹配校验（本工具最关键的部分）
---------------------------------
高德 POI 搜索**永远返回结果**，即使查询根本不存在。实测：

- ``一个根本不存在的景点XYZ`` → 返回「不存在茶铺」「根本烘焙面包店」（按词拆开模糊匹配）
- ``蒙马特高地与圣心大教堂``（region=巴黎）→ 返回**广州的**中山纪念堂

**region 参数不可靠。** 因此本工具做了两层校验，不通过就**返回失败而不是给错数据**：

1. **城市校验**（强信号）：返回的 ``cityname`` 与预期城市对不上 → 直接拒绝
2. **名称相似度**：字符覆盖率 < 0.6 → 拒绝；0.6~0.9 → 返回但标注需人工确认

合规边界（同驾车/公交接口）
--------------------------
- ✅ 接口文档「适用场景」含「无需展现地图的场景」
- ❌ 服务协议 3.5 条禁止存储/缓存 —— **只在当次会话使用，不落库**
- ⚠️ 配额 10,000 次/月、2 QPS，失败降级不重试
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.data.cities import lookup_city
from mcp_server.tools.base import env, register, tool_timeout
from mcp_server.tools.route import AMAP_KEY_ENV, AMAP_QUOTA_INFOCODE, amap_covers

AMAP_POI_URL = "https://restapi.amap.com/v5/place/text"

# 名称相似度阈值
MATCH_REJECT_BELOW = 0.60  # 低于此值 → 判定为不匹配，返回失败
MATCH_HIGH_ABOVE = 0.90  # 高于此值 → 高置信度

# 归一化时去掉的噪音（括号内容、常见后缀）
_BRACKETS = re.compile(r"[（(][^）)]*[）)]")
_STRIP_CHARS = re.compile(r"[\s·・\-—_]")


class AttractionLiveParams(BaseModel):
    name: str = Field(description="景点名称，如 成都大熊猫繁育研究基地")
    city: str = Field(description="所在城市，如 成都。用于校验匹配结果，必填")

    @field_validator("name", "city")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("不能为空")
        return v.strip()


# ---------------------------------------------------------------------------
# 匹配校验
# ---------------------------------------------------------------------------


def normalize_name(text: str) -> str:
    """归一化地名：去括号内容、去空白与分隔符。"""
    return _STRIP_CHARS.sub("", _BRACKETS.sub("", str(text or ""))).strip()


def name_similarity(query: str, candidate: str) -> float:
    """0~1 的名称相似度，用于判断高德返回的 POI 是否真的匹配查询。

    高德会「按词拆开模糊匹配」，所以不能只看有没有结果 ——
    查「一个根本不存在的景点XYZ」它会返回「不存在茶铺」。

    策略：精确相等 > 子串包含 > 字符集覆盖率（以**查询**为基准）。
    """
    q, c = normalize_name(query), normalize_name(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c:
        # 「都江堰景区」⊂「都江堰景区鱼嘴分水堤」
        return 0.95
    if c in q:
        return 0.90 * len(c) / len(q)
    # 查询的字符有多少出现在候选里（「熊猫基地」⊂「成都大熊猫繁育研究基地」的字符集）
    return len(set(q) & set(c)) / len(set(q))


def cities_conflict(expected: str, actual: str) -> bool:
    """判断返回的城市是否与预期冲突。

    实测 ``region=巴黎`` 的查询返回了广州市的结果 —— region 参数不可靠，
    必须自己校验。这里做双向包含判断（「成都市」vs「成都」视为一致）。

    **注意这只是第一道筛。** 字符串比对对县级市会误判（见 ``nearby``），
    所以真正的判据是坐标，这里判为冲突时还要过一遍坐标复核。
    """
    e, a = normalize_name(expected), normalize_name(actual)
    if not e or not a:
        return False  # 拿不到城市就不据此拒绝，交给名称相似度
    return not (e in a or a in e)


# POI 与目标城市的距离上限。超过就认为确实不是同一个地方。
NEARBY_KM = 120.0


def nearby(city: str, poi: dict[str, Any]) -> bool:
    """用坐标复核 POI 是否属于目标城市。

    **这是城市归属的可靠判据。** 高德的 ``cityname`` 对县级市填的是**上级
    行政区**：查「稻城」返回的 ``cityname`` 是「甘孜藏族自治州」，
    查「康定」同样 —— 字符串比对会判为冲突，把**完全正确**的数据拒掉。

    实测踩到：用户说「我想去稻城亚丁」，9 项查证里 5 项失败，其中
    ``query_attraction_realtime`` 拿到的其实是「甘孜稻城亚丁景区」
    （adcode 513337 正是稻城县），却因为 cityname 是州名而被拒绝。

    坐标不会有这个问题：稻城县的 POI 就在稻城坐标附近。
    """
    info = lookup_city(city)
    if info is None:
        return False
    dist = _distance_km(poi.get("location") or "", info.lat, info.lon)
    return dist is not None and dist <= NEARBY_KM


def _distance_km(location: str, lat: float, lon: float) -> float | None:
    """高德的 ``location`` 是 ``"经度,纬度"``。解析不了返回 None。"""
    if not location or "," not in location:
        return None
    lon_s, _, lat_s = location.partition(",")
    try:
        return _haversine_km(lat, lon, float(lat_s), float(lon_s))
    except ValueError:
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_poi(poi: dict[str, Any]) -> dict[str, Any]:
    """把一条高德 POI 压成我们关心的字段。"""
    biz = poi.get("business") or {}
    result: dict[str, Any] = {
        "name": poi.get("name") or "",
        "city": poi.get("cityname") or "",
        "address": poi.get("address") or "",
        "type": poi.get("type") or "",
    }
    if biz.get("rating"):
        try:
            result["rating"] = float(biz["rating"])
        except (TypeError, ValueError):
            pass
    # 开放时间：这是本工具的主要价值，含季节性/闭馆日
    if biz.get("opentime_week"):
        result["opentime"] = str(biz["opentime_week"])
    elif biz.get("opentime_today"):
        result["opentime"] = str(biz["opentime_today"])
    if biz.get("tel"):
        result["tel"] = str(biz["tel"])
    return result


def pick_best_match(
    pois: list[dict[str, Any]], *, query_name: str, query_city: str
) -> tuple[dict[str, Any] | None, float, str]:
    """从候选里挑最佳匹配。返回 ``(poi, 相似度, 拒绝原因)``。

    校验顺序：城市冲突 → 名称相似度。任一不过就返回 None。
    """
    best: dict[str, Any] | None = None
    best_score = -1.0

    for poi in pois:
        if not isinstance(poi, dict):
            continue
        score = name_similarity(query_name, poi.get("name") or "")
        if score > best_score:
            best, best_score = poi, score

    if best is None:
        return None, 0.0, "高德没有返回任何候选"

    actual_city = best.get("cityname") or ""
    # 城市名对不上时，**再用坐标复核一次** —— 高德对县级市填的是上级行政区名
    # （查「稻城」返回「甘孜藏族自治州」），光比字符串会把正确数据拒掉。
    if cities_conflict(query_city, actual_city) and not nearby(query_city, best):
        # 这是最强的不匹配信号：查巴黎的景点却返回广州的结果
        return (
            None,
            best_score,
            f"城市不符：查询「{query_city}」，返回的是「{actual_city}」的"
            f"「{best.get('name')}」—— 高德的 region 参数不可靠，不做兜底采信",
        )

    if best_score < MATCH_REJECT_BELOW:
        return (
            None,
            best_score,
            f"名称不匹配（相似度 {best_score:.0%}）：查询「{query_name}」，"
            f"最接近的是「{best.get('name')}」",
        )

    return best, best_score, ""


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _fetch_poi(params: AttractionLiveParams) -> tuple[dict | None, str]:
    key = env(AMAP_KEY_ENV)
    if not key:
        return None, "未配置 AMAP_API_KEY"

    try:
        with httpx.Client(timeout=tool_timeout()) as client:
            resp = client.get(
                AMAP_POI_URL,
                params={
                    "key": key,
                    "keywords": params.name,
                    "region": params.city,
                    "show_fields": "business",  # 评分/开放时间/电话都在这里
                    "page_size": "5",
                    "output": "JSON",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.TimeoutException:
        return None, "高德请求超时"
    except httpx.HTTPError as exc:
        return None, f"高德请求失败: {type(exc).__name__}"
    except ValueError:
        return None, "高德返回的不是合法 JSON"

    if payload.get("status") != "1":
        info = payload.get("info", "未知错误")
        infocode = str(payload.get("infocode", ""))
        if infocode == AMAP_QUOTA_INFOCODE:
            return None, "高德配额已用尽（个人账号 10000 次/月）"
        if infocode == "10001":
            return None, "AMAP_API_KEY 无效"
        return None, f"高德返回错误: {info}（infocode={infocode}）"

    return payload, ""


def summarize_attraction_live(data: dict[str, Any]) -> str:
    """形如：``成都大熊猫繁育研究基地 · 评分 4.7 · 07:30-18:00…``。

    开放时间原文很长（含季节性、闭馆日），这里截断 —— 完整内容在计划正文里。
    """
    name = str(data.get("matched_name") or "").strip()
    if not name:
        return ""

    bits: list[str] = []
    rating = data.get("rating")
    if rating:
        bits.append(f"评分 {rating}")
    opentime = " ".join(str(data.get("opentime") or "").split())
    if opentime:
        bits.append(opentime[:26] + ("…" if len(opentime) > 26 else ""))

    return f"{name} · {' · '.join(bits)}" if bits else name


@register(
    name="query_attraction_realtime",
    description=(
        "实时查询某个景点的**最新开放时间**与评分（高德 POI）。"
        "用于核对本地景点库可能过时的开放时间 —— 尤其季节调整、周一闭馆、"
        "节假日例外这类静态种子库反映不了的信息。"
        "需要配置 AMAP_API_KEY，且**只覆盖中国境内**。"
        "**不提供门票价格**（高德该字段覆盖率仅 5%）。"
        "注意：返回的 match_confidence 表示匹配可靠度，"
        "medium 时需结合 matched_name 自行确认是不是同一个地方。"
    ),
    params_model=AttractionLiveParams,
    idempotent=True,
    tags=["外部API", "景点"],
    summarize=summarize_attraction_live,
)
def query_attraction_realtime(params: AttractionLiveParams) -> ToolResult:
    # 先确认城市能解析 —— 顺便校验用户给的城市名是有效的
    city = lookup_city(params.city)
    if city is None:
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"无法解析城市「{params.city}」。本工具只覆盖中国境内城市。",
        )

    # **调用前判掉境外。** 不判的话高德会静默降级成全国模糊搜索，
    # 返回一堆同名但不同城的 POI（查「大英博物馆」返回遂宁市的
    # 「大英汉陶博物馆」、查「伦敦塔桥」返回北京世界公园的微缩景观），
    # 白烧一次配额，最后抛一句让人摸不着头脑的「城市不符」。
    if not amap_covers(city):
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"本工具（高德实时 POI）只覆盖中国大陆与港澳，"
            f"「{city.name_zh}」位于{city.country}，不在覆盖范围内。\n"
            "**这是覆盖范围问题，不是这个景点不存在**，不要据此判断景点信息。\n"
            "请改用：\n"
            "1. `query_attractions_db` —— 本地景点库（含 305 个城市，"
            "境外热门城市如东京 / 巴黎 / 伦敦 / 纽约 / 新加坡 / 曼谷 / 首尔都在其中）；\n"
            "2. `fetch_webpage` 抓取该景点的官网或官方页面；\n"
            "3. 都拿不到时，在计划里标注「⚠️ 开放时间与票价需自行核实」。",
            tool="query_attraction_realtime",
        )

    payload, error = _fetch_poi(params)
    if payload is None:
        # 不重试：配额有限，重试只会更快烧完
        return ToolResult.failure(ERR_UPSTREAM_ERROR, error, tool="query_attraction_realtime")

    pois = payload.get("pois") or []
    best, score, reject_reason = pick_best_match(
        pois, query_name=params.name, query_city=city.name_zh
    )
    if best is None:
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"未能可靠匹配到「{params.name}」（{city.name_zh}）：{reject_reason}。\n"
            "高德 POI 搜索会「按词拆开模糊匹配」，**永远返回结果**，"
            "因此不匹配时宁可报错也不给错数据。\n"
            "请改用 query_attractions_db 的本地数据，"
            "或在计划中标注「⚠️ 开放时间需自行核实」。",
            tool="query_attraction_realtime",
        )

    data = parse_poi(best)
    confidence = "high" if score >= MATCH_HIGH_ABOVE else "medium"

    result: dict[str, Any] = {
        "query": {"name": params.name, "city": city.name_zh},
        "matched_name": data["name"],
        "matched_city": data["city"],
        "matched_address": data["address"],
        "match_confidence": confidence,
        "match_score": round(score, 2),
        "rating": data.get("rating"),
        "opentime": data.get("opentime"),
        "tel": data.get("tel"),
        "note": (
            "开放时间与评分来自高德实时查询，可能有延迟。"
            "**票价不在本工具范围内**（高德覆盖率仅 5%）—— "
            "门票价格请以景区官方渠道为准。"
        ),
    }
    if confidence == "medium":
        result["match_warning"] = (
            f"匹配置信度为 medium（相似度 {score:.0%}）："
            f"查询「{params.name}」，高德返回「{data['name']}」。"
            "**请自行确认是同一个地方再用**，否则可能给出错误信息。"
        )

    return ToolResult.success(result, source="amap:poi")
