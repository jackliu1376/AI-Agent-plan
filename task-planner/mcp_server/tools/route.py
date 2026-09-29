"""工具 T6：estimate_route —— 两地交通距离与耗时估算。

用内置城市坐标 + Haversine 计算直线距离，再按交通方式系数折算实际里程与耗时。
完全离线，零外部依赖，因此也是演示时最稳的一个工具。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ToolResult
from mcp_server.data.cities import CITIES, lookup_city, supported_city_names
from mcp_server.tools.base import register

# 城市坐标来自 mcp_server/data/cities.py（与天气工具共用同一份事实来源）
CITY_COORDS: dict[str, tuple[float, float]] = {c.name_zh: (c.lat, c.lon) for c in CITIES}

# 交通方式：(里程系数, 平均时速 km/h, 附加耗时 h, 每公里单价 ¥)
MODE_PROFILE: dict[str, tuple[float, float, float, float]] = {
    "walk": (1.2, 4.5, 0.0, 0.0),
    "bike": (1.25, 14.0, 0.1, 0.0),
    "drive": (1.30, 75.0, 0.3, 1.1),
    "bus": (1.35, 55.0, 0.7, 0.45),
    "train": (1.22, 160.0, 1.2, 0.42),
    "hsr": (1.20, 250.0, 1.0, 0.46),  # 高铁
    "flight": (1.15, 750.0, 2.5, 0.75),  # 含值机安检
}

EARTH_RADIUS_KM = 6371.0


class RouteParams(BaseModel):
    origin: str = Field(description="出发城市，如 上海")
    destination: str = Field(description="目的城市，如 成都")
    mode: str = Field(
        default="hsr",
        description="交通方式：walk / bike / drive / bus / train / hsr / flight",
    )

    @field_validator("origin", "destination")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("城市名不能为空")
        return v.strip()

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in MODE_PROFILE:
            raise ValueError(f"不支持的交通方式 {v!r}，可选: {', '.join(MODE_PROFILE)}")
        return v


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点球面距离（公里）。"""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


@register(
    name="estimate_route",
    description=(
        "估算两个城市之间的交通距离、耗时与费用。"
        "支持国内城市与主要国际城市（如 纽约 / 东京 / 巴黎），中英文名均可。"
        "用于跨城行程的转场安排。基于内置城市坐标离线计算，结果为量级估算而非精确导航。"
    ),
    params_model=RouteParams,
    idempotent=True,
    tags=["本地计算", "交通"],
)
def estimate_route(params: RouteParams) -> ToolResult:
    # 用共享城市表解析，支持中文名 / 英文名 / 别名（如 New York -> 纽约）
    origin = lookup_city(params.origin)
    destination = lookup_city(params.destination)
    missing = [
        raw
        for raw, city in ((params.origin, origin), (params.destination, destination))
        if city is None
    ]
    if missing:
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"内置坐标库中没有这些城市: {', '.join(missing)}。"
            f"已支持 {len(supported_city_names())} 个城市: "
            f"{', '.join(supported_city_names())}",
        )

    assert origin is not None and destination is not None  # for type checkers
    origin_name, dest_name = origin.name_zh, destination.name_zh

    if origin_name == dest_name:
        return ToolResult.success(
            {
                "origin": origin_name,
                "destination": dest_name,
                "mode": params.mode,
                "distance_km": 0.0,
                "duration_hours": 0.0,
                "estimated_cost": 0.0,
                "note": "同城，无需城际交通。",
            },
            source="offline:city-coords",
        )

    straight = haversine_km(
        CITY_COORDS[origin_name], CITY_COORDS[dest_name]
    )
    detour, speed, overhead, unit_price = MODE_PROFILE[params.mode]
    distance = straight * detour
    duration = distance / speed + overhead
    cost = distance * unit_price

    return ToolResult.success(
        {
            "origin": origin_name,
            "destination": dest_name,
            "mode": params.mode,
            "straight_line_km": round(straight, 1),
            "distance_km": round(distance, 1),
            "duration_hours": round(duration, 1),
            "estimated_cost": round(cost, 2),
            "note": "基于直线距离折算的量级估算，实际请以购票平台为准。",
        },
        source="offline:city-coords",
    )
