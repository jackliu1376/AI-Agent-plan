"""工具 T3：query_attractions_db —— 查询本地景点库。

按城市 + 标签 + 人均价格上限筛选，返回结构化景点列表。
这是与「天气 API」功能互异的第二个技能（本地数据库查询）。

时效性
------
``price`` / ``open_hours`` / ``rating`` 是**时效性数据**：票价会调整、
开放时间会变更、景点会歇业。每条记录带 ``verified_at``（数据整理月份），
返回值里还有 ``freshness_note``，Agent 必须把它转述进计划 ——
让用户知道"这个数字有多新"，而不是默认它是实时的。

数据来源
--------
库里混了三批来源，由 ``source`` 字段区分，**字段可信度不同**：

- ``manual`` —— 人工逐条核对，所有字段可直接引用
- ``amap`` —— 高德 POI 实抓：名称/评分/开放时间可靠，**票价是估算值**
- ``wikidata`` —— Wikidata 实抓：只有名称与类型可靠，票价与评分都是估算

返回值里透出 ``source``，模型据此调整措辞的确定性。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ToolResult
from mcp_server.data.cities import lookup_city
from mcp_server.data.seed import (
    connect,
    covered_cities,
    data_verified_at,
    freshness_disclaimer,
)
from mcp_server.tools.base import register

# 官方查询渠道：**刻意返回渠道类型而非具体网址**。
#
# 理由：
# 1. 网址会失效，渠道类型不会；
# 2. 我们无法逐一核实几百个景点的官网 —— 而**编造网址正是本项目要避免的事**；
# 3. 国内景区售票与预约绝大多数走官方微信公众号 / 小程序，这个指引是准确且可执行的。
#
# 若某个城市确有稳定的官方统一平台，在此覆盖。只写有把握的，宁可少不可错。
CHANNEL_OVERRIDES: dict[str, str] = {}

CHANNEL_CN = "景区官方微信公众号 / 官方小程序（微信搜索景区名），或景区官网"
CHANNEL_INTL = "景点官网（official website）或官方票务渠道"


def official_channel_for(city: str, country: str) -> str:
    """给出该地景点的官方核实渠道。"""
    override = CHANNEL_OVERRIDES.get(city)
    if override:
        return override
    if country.startswith("中国"):
        return CHANNEL_CN
    return CHANNEL_INTL


class AttractionsParams(BaseModel):
    city: str = Field(description="城市名，如 成都")
    tags: list[str] = Field(default_factory=list, description="标签过滤，如 ['亲子','自然']")
    max_price: float | None = Field(
        default=None,
        ge=0,
        description=(
            "人均价格上限。**注意单位是当地货币，不是人民币** —— "
            "例如东京的 1000 指 1000 日元。需要人民币口径请先自行换算。"
        ),
    )
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限")
    kid_friendly_only: bool = Field(default=False, description="只看亲子友好")

    @field_validator("city")
    @classmethod
    def _city_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("city 不能为空")
        return v.strip()

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, v):  # noqa: ANN001
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.replace("，", ",").split(",") if t.strip()]
        return v


def _coverage_note() -> str:
    """从数据库实时生成覆盖说明。

    手写的覆盖描述会随数据增长而过时 —— 这里曾经写着「5 国内 + 7 国际」，
    而实际已经 42 城。**这段文字会直接进入 LLM 的 tools 参数**，
    过时描述会让模型误以为目标城市没有数据，从而放弃调用本工具。
    """
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT country, COUNT(DISTINCT city) AS n FROM attractions "
                "GROUP BY country ORDER BY n DESC, country"
            ).fetchall()
    except Exception:  # noqa: BLE001 - 描述生成失败不该阻断工具注册
        return ""

    if not rows:
        return ""

    total = sum(int(r["n"]) for r in rows)
    # 只列有一定规模的国家，其余合并 —— 这段文字**每次请求都会进 LLM 的
    # tools 参数**，扩到 250 城后逐个列国家会变成 200+ 字符的清单，
    # 而其中大半是「某国 1 城」。模型真正需要的是「大致覆盖哪儿」。
    detail = "、".join(f"{r['country']} {r['n']}" for r in rows if int(r["n"]) >= 3)
    minor = [r for r in rows if int(r["n"]) < 3]
    if minor:
        detail += f"、另 {len(minor)} 个国家/地区各 1–2 城"
    return f"当前库覆盖 {total} 个城市（{detail}）。"


def _row_to_dict(row: sqlite3.Row) -> dict:
    # 防御性取列：自定义库（ATTRACTIONS_DB）不会随结构变化自动重建，
    # 旧库可能没有 source 列。
    keys = row.keys()
    return {
        "name": row["name"],
        "country": row["country"],
        "tags": [t for t in row["tags"].split(",") if t],
        "price": row["price"],
        "currency": row["currency"],
        "duration_hours": row["duration_hours"],
        "rating": row["rating"],
        "kid_friendly": bool(row["kid_friendly"]),
        "open_hours": row["open_hours"],
        "note": row["note"],
        # 时效性标注：让模型知道这个数字是什么时候整理的
        "verified_at": row["verified_at"],
        # 数据来源。**必须透出** —— 不同来源的字段可信度不同，
        # 模型据此决定措辞的确定性（manual 可直接引用，amap 的票价要加「约」）。
        "source": row["source"] if "source" in keys else "manual",
    }


_CURRENCY_SYMBOL = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥", "GBP": "£", "KRW": "₩"}


def summarize_attractions(data: dict[str, Any]) -> str:
    """形如：``匹配到 12 个景点，人均 ¥0–180``。"""
    items = data.get("items") or []
    if not items:
        return ""

    prices = [i["price"] for i in items if isinstance(i.get("price"), (int, float))]
    head = f"匹配到 {len(items)} 个景点"
    if not prices:
        return head

    symbol = _CURRENCY_SYMBOL.get(str(data.get("currency") or ""), "")
    low, high = round(min(prices)), round(max(prices))
    amount = f"{symbol}{low}" if low == high else f"{symbol}{low}–{high}"
    return f"{head}，人均 {amount}"


def _city_candidates(city: str) -> list[str]:
    """把用户输入的城市名展开成库里可能用的名字。

    **这是必须的一步。** 库里存的是策展表的主名（``稻城``），而用户会说别名
    （``稻城亚丁``）。天气和交通工具都走 ``lookup_city`` 解析，所以别名没问题；
    但本工具原来是直接 ``WHERE city = ?`` 精确匹配 —— 于是同一个地名
    「稻城亚丁」在天气、交通里能用，在景点库里 NOT_FOUND。

    实测踩到：用户说「我想去稻城亚丁」，9 项查证里 5 项失败，其中就包括
    景点库。而稻城的 20 条景点**确实在库里**。

    返回主名优先、原始输入兜底 —— 万一解析出来的主名和库里对不上，
    原来能查的查询也不会被搞坏。
    """
    resolved = lookup_city(city)
    if resolved is not None and resolved.name_zh != city:
        return [resolved.name_zh, city]
    return [city]


@register(
    name="query_attractions_db",
    description=(
        "从本地景点数据库中查询符合条件的景点（支持城市、标签、人均价格上限、是否亲子友好）。"
        "覆盖中国全部省级行政区（含港澳台）与主要国际城市。"
        + _coverage_note()
        +         "注意：价格是**当地货币**，由 currency 字段标注；如需折算成人民币请再调用 convert_currency。"
        "数据为本地离线库，不需要网络。"
        "**price / open_hours / rating 为时效性数据**：每条记录带 verified_at（数据整理月份），"
        "引用这些字段时必须在计划中转述核对时间，并提示用户出行前通过官方渠道复核。"
        "**还要看 source 字段**：manual = 人工核对，可直接引用；"
        "amap = 高德实抓，评分与开放时间可靠但**票价是估算值**，须加「约」；"
        "wikidata = Wikidata 实抓，**只有名称和类型可靠**，票价与评分均为估算，"
        "引用时必须明确说明是估算。"
    ),
    params_model=AttractionsParams,
    idempotent=True,
    tags=["本地数据库", "景点"],
    summarize=summarize_attractions,
)
def query_attractions_db(params: AttractionsParams) -> ToolResult:
    candidates = _city_candidates(params.city)
    sql = "SELECT * FROM attractions WHERE city IN ({})".format(
        ", ".join("?" for _ in candidates)
    )
    args: list = list(candidates)

    if params.max_price is not None:
        sql += " AND price <= ?"
        args.append(params.max_price)

    if params.kid_friendly_only:
        sql += " AND kid_friendly = 1"

    sql += " ORDER BY rating DESC, price ASC"

    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()

    # 标签过滤在 Python 侧做（标签是逗号分隔字符串，SQL 里做交集不划算）
    if params.tags:
        wanted = {t.strip() for t in params.tags if t.strip()}
        rows = [r for r in rows if wanted & set(r["tags"].split(","))]

    items = [_row_to_dict(r) for r in rows[: params.limit]]

    if not items:
        # 只有失败路径才需要覆盖清单（此前每次查询都跑一遍全表 DISTINCT）
        #
        # **不要在这里列全部城市名。** 扩到 250 城之后，`', '.join(covered)`
        # 会产出约 2000 字符的提示，白白吃掉模型的上下文 —— 而模型需要的只是
        # 「大概覆盖哪些地方 + 不在里面就如实说」。国家汇总就够了。
        #
        # **区分两种「没结果」**：城市压根没收录 vs 城市有但筛选条件太窄。
        # 混在一起说会误导模型 —— 前者该换数据源，后者该放宽条件。
        # （前车之鉴：高德的「覆盖不到」被写成了「没有交通方案」，把用户带偏。）
        if params.city.strip() not in covered_cities():
            reason = (
                f"本地景点库**没有收录「{params.city}」**。"
                "这是数据覆盖问题，不代表该城市没有景点，更不要据此编造景点与票价。"
            )
        else:
            reason = (
                f"「{params.city}」有数据，但**当前筛选条件筛空了**"
                f"（tags={params.tags}, max_price={params.max_price}, "
                f"kid_friendly_only={params.kid_friendly_only}）。可以放宽条件再查一次。"
            )
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"{reason}\n"
            f"当前库覆盖 {_coverage_note()}"
            "若目标城市不在其中，请如实告知用户「本地景点库无该城市数据」，"
            "不要凭记忆编造景点与票价。",
            source="local:attractions.db",
        )

    totals: dict[str, float] = {}
    for item in items:
        cur = item["currency"]
        totals[cur] = round(totals.get(cur, 0.0) + item["price"], 2)

    country = items[0]["country"]
    return ToolResult.success(
        {
            "city": params.city,
            "country": country,
            "currency": items[0]["currency"],
            "count": len(items),
            "items": items,
            "totals_by_currency": totals,
            "total_hours_if_all_visited": round(sum(i["duration_hours"] for i in items), 1),
            "note": (
                "价格为当地货币。若需人民币口径，请对 totals_by_currency 调用 convert_currency 换算。"
            ),
            # -- 时效性声明（必须转述进计划）----------------------------------
            "data_verified_at": data_verified_at(),
            "freshness_note": freshness_disclaimer(),
            "official_channel": official_channel_for(params.city, country),
        },
        source="local:attractions.db",
    )
