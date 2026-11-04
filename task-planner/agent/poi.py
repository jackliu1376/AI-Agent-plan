"""从工具调用结果里收集可上地图的景点坐标。

为什么单独一个模块
------------------
坐标是 ``query_attraction_realtime`` 的**副产物**，不是计划的一部分。

- 放进 ``Plan`` schema 会让模型必须输出坐标 —— 而模型并不知道真实坐标，
  只会编造，比没有更糟。``Plan`` 同时是评测基准，加字段还会让历史样本失效。
- 放进 ``LoopTrace`` 又不对：trace 记的是「调用了几次、快不快」，
  不是「数据长什么样」。

所以走第三条通道：编排循环边跑边收集，跟 ``plan_ready`` 事件一起推给前端。

合规边界
--------
高德服务协议 3.5 条禁止存储 / 缓存 POI 数据。本模块产出的坐标：

- ✅ 只存在内存里，随会话生命周期结束而消失
- ❌ 不写 ``session_db``，不落任何磁盘
- ❌ 不做跨会话复用

因此**历史会话的地图是空的** —— 这是刻意的取舍，不是 bug。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "PoiMarker",
    "assign_days",
    "collect_markers",
    "extract_marker",
]

# 只认这一个工具：其它工具要么没有坐标，要么坐标是城市中心（不是景点）。
POI_TOOL = "query_attraction_realtime"

# 日次标记。覆盖模型实际会写的几种形式：
#   ### D1 2026-11-07 / **D1** / 第 1 天 / 第一天 / Day 1 / DAY1
# 不匹配「D1」出现在句子中间的情况（如「从 D1 出发」），所以要求行首或
# 标题标记之后 —— 逐日路线的日次几乎总在行首。
_DAY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^#{0,6}\s*\**\s*D\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"^#{0,6}\s*\**\s*第\s*(\d{1,2})\s*[天日]", re.IGNORECASE),
    re.compile(r"^#{0,6}\s*\**\s*Day\s*(\d{1,2})\b", re.IGNORECASE),
)

# 中文数字日次（第一天 / 第二天 …），只处理 1-10 —— 再长的行程地图也没意义
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_DAY = re.compile(r"^#{0,6}\s*\**\s*第\s*([一二三四五六七八九十])\s*[天日]")

# 名称归一化时去掉的字符（空白、分隔符、括号）
_NORM = re.compile(r"[\s·・\-—_（）()「」【】\[\]]+")


@dataclass(frozen=True)
class PoiMarker:
    """地图上的一个点。

    ``query`` 是模型查询时用的名字（``西湖``），``name`` 是高德返回的规范名
    （``杭州西湖风景名胜区``）。两者都要留：前者用来在计划正文里找它属于哪天，
    后者用来在地图上显示 —— 用户认的是后者。
    """

    name: str
    query: str
    city: str
    lon: float
    lat: float
    day: str = ""
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "query": self.query,
            "city": self.city,
            "lon": self.lon,
            "lat": self.lat,
            "day": self.day,
            "order": self.order,
        }


def _normalize(text: str) -> str:
    return _NORM.sub("", str(text or ""))


def _locate(chunk: str, name: str) -> int:
    """在归一化后的正文片段里定位一个景点，返回字符位置；找不到返回 -1。

    **为什么要退化匹配。** 模型查询景点时习惯给全名，写正文时却用简称：

    - 查 ``雷峰塔景区``，正文写「下午：苏堤 → 花港观鱼 → 雷峰塔」
    - 查 ``南宋德寿宫遗址博物馆``，正文写「09:20 到德寿宫」

    精确匹配会漏掉这一类，景点就落到「未分组」里 —— 地图上点还在，但没法
    按天筛选。所以精确匹配失败后，退化为「名字的任意连续子串是否出现在正文」，
    从长到短找，第一个命中即返回（优先用更长的匹配，减少误判）。

    ``min_len`` 取 ``min(3, len(name))``：像「西湖」这种两字名，只认全名，
    否则「杭州」两个字会匹配到正文里任何一处，把点分到错误的那天。
    """
    normalized = _normalize(name)
    if not normalized:
        return -1

    position = chunk.find(normalized)
    if position >= 0:
        return position

    shortest = min(3, len(normalized))
    for size in range(min(len(normalized) - 1, 8), shortest - 1, -1):
        for start in range(0, len(normalized) - size + 1):
            position = chunk.find(normalized[start : start + size])
            if position >= 0:
                return position
    return -1


def extract_marker(tool_name: str, data: dict[str, Any] | None) -> PoiMarker | None:
    """从一次工具调用的返回里取出坐标。不是景点工具或没有坐标时返回 None。

    调用方传的是 ``ToolResult.data``，失败时它是 None —— 这里一并挡掉。
    """
    if tool_name != POI_TOOL or not data:
        return None

    location = data.get("location")
    if not isinstance(location, dict):
        return None
    try:
        lon = float(location["lon"])
        lat = float(location["lat"])
    except (KeyError, TypeError, ValueError):
        return None

    name = str(data.get("matched_name") or "").strip()
    # 先落到变量再 isinstance 收窄 —— 写成一行的三元表达式时，
    # mypy 无法把 isinstance 的判断和取值关联起来，会把结果当成 Any
    raw_query = data.get("query")
    query_block = raw_query if isinstance(raw_query, dict) else {}
    query = str(query_block.get("name") or name).strip()
    city = str(data.get("matched_city") or query_block.get("city") or "").strip()
    if not name:
        return None

    return PoiMarker(name=name, query=query, city=city, lon=lon, lat=lat)


def collect_markers(
    existing: list[PoiMarker], marker: PoiMarker | None
) -> list[PoiMarker]:
    """追加一个点，按名字去重。

    同一景点在初版和修订里可能被查两次（修订会重跑工具），去重避免地图上
    出现两个完全重合的点 —— 那看起来像 bug。
    """
    if marker is None:
        return existing
    key = _normalize(marker.name) or f"{marker.lon:.5f},{marker.lat:.5f}"
    for item in existing:
        other = _normalize(item.name) or f"{item.lon:.5f},{item.lat:.5f}"
        if other == key:
            return existing
    return [*existing, marker]


def _day_of(line: str) -> str | None:
    """判断这一行是不是日次标题，是则返回 ``D1`` 形式，否则 None。"""
    stripped = line.strip()
    if not stripped:
        return None
    for pattern in _DAY_PATTERNS:
        hit = pattern.match(stripped)
        if hit:
            return f"D{int(hit.group(1))}"
    cn = _CN_DAY.match(stripped)
    if cn:
        num = _CN_NUM.get(cn.group(1))
        if num is not None:
            return f"D{num}"
    return None


# 章节级标题（一级 / 二级）。日次段落遇到它就结束 —— 否则最后一个 D 段落会
# 一路吃到文档末尾，把「计划表」「预算」「风险」全算进去，那些地方提到景点
# 的次数反而更多，归属就判错了。
# 只认 `#` 和 `##`：`###` 通常是 D 段落自己的子标题（如「阶段二：行程执行」），
# 属于段内内容，不能截断。
_SECTION_BREAK = re.compile(r"^#{1,2}\s")


def _day_sections(markdown: str) -> list[tuple[str, int, int]]:
    """把正文切成 ``[(day, start, end)]``，按出现顺序。

    段落终点取「下一个日次标记」和「下一个章节标题」中**较近的那个**。

    没有日次标记时返回空列表 —— 调用方据此把所有点归为「未分组」，
    而不是硬塞进一个假的 D1。
    """
    lines = markdown.splitlines()
    offsets: list[tuple[int, int]] = []  # (行首字符偏移, 行号)
    cursor = 0
    for index, line in enumerate(lines):
        offsets.append((cursor, index))
        cursor += len(line) + 1

    marks: list[tuple[str, int]] = []
    breaks: list[int] = []
    for start, index in offsets:
        stripped = lines[index].strip()
        day = _day_of(stripped)
        if day is not None:
            marks.append((day, start))
        elif _SECTION_BREAK.match(stripped):
            breaks.append(start)

    sections: list[tuple[str, int, int]] = []
    for i, (day, start) in enumerate(marks):
        next_day = marks[i + 1][1] if i + 1 < len(marks) else len(markdown)
        next_break = next((b for b in breaks if b > start), len(markdown))
        sections.append((day, start, min(next_day, next_break)))
    return sections


def _occurrences(chunk: str, name: str) -> int:
    """名字在片段里出现了几次。匹配规则与 ``_locate`` 一致（含简称退化）。"""
    normalized = _normalize(name)
    if not normalized:
        return 0

    total = chunk.count(normalized)
    if total > 0:
        return total

    shortest = min(3, len(normalized))
    for size in range(min(len(normalized) - 1, 8), shortest - 1, -1):
        for start in range(0, len(normalized) - size + 1):
            total = chunk.count(normalized[start : start + size])
            if total > 0:
                return total
    return 0


def assign_days(markdown: str, markers: list[PoiMarker]) -> list[PoiMarker]:
    """根据计划正文判断每个景点属于哪一天。

    为什么不在 ``Plan`` 里加一个 ``days`` 字段
    ------------------------------------------
    模型完全可能把「西湖」写成「杭州西湖」「西湖景区」「西湖风景区」，
    让它在结构化字段里再抄一遍名字，等于多一个不一致的来源。

    正文本来就是唯一事实来源 —— 直接在里面找名字，找到哪段算哪天。

    匹配用 ``query``（模型自己写的名字）而不是 ``name``（高德规范名）：
    正文里写的是前者。两者都试，``query`` 优先。

    **判据是「在哪天出现得最多」，不是「第一次出现在哪天」。**
    实测踩到：正文开头有一句「路线按 D1 西湖西线、D2 老城与德寿宫安排」，
    德寿宫在这一句里被提到了一次 —— 按「首次出现」会把它分到 D1，
    而它真正属于 D2。按次数投票就没有这个问题：D2 段落里它会反复出现。
    """
    if not markers:
        return []
    if not markdown.strip():
        return [replace(m, day="", order=i) for i, m in enumerate(markers)]

    sections = _day_sections(markdown)
    if not sections:
        return [replace(m, day="", order=i) for i, m in enumerate(markers)]

    # 每段只归一化一次 —— 下面每个景点都要在每段里找，重复归一化是纯浪费
    chunks = [(day, _normalize(markdown[start:end])) for day, start, end in sections]

    assigned: dict[int, tuple[str, int]] = {}  # marker 下标 → (day, 段内位置)
    for index, marker in enumerate(markers):
        # query 优先：正文里写的是模型自己起的名字，不是高德的规范名
        keys = list(
            dict.fromkeys(k for k in (_normalize(marker.query), _normalize(marker.name)) if k)
        )
        best: tuple[int, int, str, int] | None = None  # (出现次数, -段序, day, 位置)

        # 第一轮：精确匹配。只要有一段出现过完整名字，就不再退化。
        #
        # 为什么必须分两轮：退化匹配会拿 3 字子串去凑，而「博物馆」这种
        # 通用后缀到处都是。实测踩到 —— 「南宋德寿宫遗址博物馆」在 D1 完整
        # 出现 1 次，但 D2 的标题写着「西湖南线 + 博物馆」、正文还有
        # 「杭州博物馆」，「博物馆」出现 2 次，退化投票就把归属判到了 D2。
        for order, (day, chunk) in enumerate(chunks):
            count = max((chunk.count(k) for k in keys), default=0)
            if count == 0:
                continue
            positions = [chunk.find(k) for k in keys if k in chunk]
            candidate = (count, -order, day, min(positions) if positions else 0)
            if best is None or candidate[:2] > best[:2]:
                best = candidate

        # 第二轮：正文里用简称时才退化（查「雷峰塔景区」，正文写「雷峰塔」）
        if best is None:
            for order, (day, chunk) in enumerate(chunks):
                count = max(
                    _occurrences(chunk, marker.query),
                    _occurrences(chunk, marker.name),
                )
                if count == 0:
                    continue
                position = _locate(chunk, marker.query)
                if position < 0:
                    position = _locate(chunk, marker.name)
                candidate = (count, -order, day, max(position, 0))
                if best is None or candidate[:2] > best[:2]:
                    best = candidate

        if best is not None:
            assigned[index] = (best[2], best[3])

    result: list[PoiMarker] = []
    for index, marker in enumerate(markers):
        day, pos = assigned.get(index, ("", index))
        result.append(replace(marker, day=day, order=pos))

    # 按「天 → 段内出现顺序」排序，让前端拿到的就是推荐游览顺序
    result.sort(key=lambda m: (m.day or "Z", m.order))
    return [replace(m, order=i) for i, m in enumerate(result)]
