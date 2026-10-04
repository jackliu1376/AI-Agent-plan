"""工具 T3：query_attractions_db —— 查询本地景点库。

按城市 + 标签 + 人均价格上限筛选，返回结构化景点列表。
这是与「天气 API」功能互异的第二个技能（本地数据库查询）。

时效性
------
``price`` / ``open_hours`` / ``rating`` 是**时效性数据**：票价会调整、
开放时间会变更、景点会歇业。每条记录带 ``verified_at``（人工核对月份），
返回值里还有 ``freshness_note``，Agent 必须把它转述进计划 ——
让用户知道"这个数字有多新"，而不是默认它是实时的。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ToolResult
from mcp_server.data.seed import connect, data_verified_at, freshness_disclaimer
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
    detail = "、".join(f"{r['country']} {r['n']}" for r in rows)
    return f"当前库覆盖 {total} 个城市（{detail}）。"


def _row_to_dict(row: sqlite3.Row) -> dict:
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
        # 时效性标注：让模型知道这个数字是什么时候核对的
        "verified_at": row["verified_at"],
    }


@register(
    name="query_attractions_db",
    description=(
        "从本地景点数据库中查询符合条件的景点（支持城市、标签、人均价格上限、是否亲子友好）。"
        "覆盖中国全部省级行政区（含港澳台）与主要国际城市。"
        + _coverage_note()
        + "注意：价格是**当地货币**，由 currency 字段标注；如需折算成人民币请再调用 convert_currency。"
        "数据为本地离线库，不需要网络。"
        "**price / open_hours / rating 为时效性数据**：每条记录带 verified_at（人工核对月份），"
        "引用这些字段时必须在计划中转述核对时间，并提示用户出行前通过官方渠道复核。"
    ),
    params_model=AttractionsParams,
    idempotent=True,
    tags=["本地数据库", "景点"],
)
def query_attractions_db(params: AttractionsParams) -> ToolResult:
    sql = "SELECT * FROM attractions WHERE city = ?"
    args: list = [params.city]

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
        with connect() as conn:
            covered = sorted(
                {r["city"] for r in conn.execute("SELECT DISTINCT city FROM attractions")}
            )
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"没有符合条件的景点（city={params.city}, tags={params.tags}, "
            f"max_price={params.max_price}）。"
            f"注意 max_price 比较的是**当地货币**数值。"
            f"本地景点库已覆盖 {len(covered)} 个城市：{', '.join(covered)}。"
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
