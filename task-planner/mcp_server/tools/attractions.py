"""工具 T3：query_attractions_db —— 查询本地景点库。

按城市 + 标签 + 人均价格上限筛选，返回结构化景点列表。
这是与「天气 API」功能互异的第二个技能（本地数据库查询）。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ToolResult
from mcp_server.data.seed import connect
from mcp_server.tools.base import register


class AttractionsParams(BaseModel):
    city: str = Field(description="城市名，如 成都")
    tags: list[str] = Field(default_factory=list, description="标签过滤，如 ['亲子','自然']")
    max_price: float | None = Field(default=None, ge=0, description="人均价格上限（元）")
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
    }


@register(
    name="query_attractions_db",
    description=(
        "从本地景点数据库中查询符合条件的景点（支持城市、标签、人均价格上限、是否亲子友好）。"
        "覆盖国内城市（成都/杭州/北京/西安/三亚）与主要国际城市（纽约/东京/巴黎/伦敦/新加坡/曼谷/首尔）。"
        "注意：价格是**当地货币**，由 currency 字段标注；如需折算成人民币请再调用 convert_currency。"
        "数据为本地离线库，不需要网络。"
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
        covered = sorted(
            {r["city"] for r in conn.execute("SELECT DISTINCT city FROM attractions")}
        )

    # 标签过滤在 Python 侧做（标签是逗号分隔字符串，SQL 里做交集不划算）
    if params.tags:
        wanted = {t.strip() for t in params.tags if t.strip()}
        rows = [r for r in rows if wanted & set(r["tags"].split(","))]

    items = [_row_to_dict(r) for r in rows[: params.limit]]

    if not items:
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

    return ToolResult.success(
        {
            "city": params.city,
            "country": items[0]["country"],
            "currency": items[0]["currency"],
            "count": len(items),
            "items": items,
            "totals_by_currency": totals,
            "total_hours_if_all_visited": round(sum(i["duration_hours"] for i in items), 1),
            "note": (
                "价格为当地货币。若需人民币口径，请对 totals_by_currency 调用 convert_currency 换算。"
            ),
        },
        source="local:attractions.db",
    )
