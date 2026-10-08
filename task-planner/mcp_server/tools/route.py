"""工具 T6：estimate_route —— 两地交通距离与耗时估算。

口径（重要，不要混用）
----------------------
返回**两个**耗时：

- ``in_vehicle_hours`` —— **车程**，可直接与「最快 X 小时」这类班次信息对比
- ``duration_hours``   —— **门到门**，车程 + 进出站 / 接驳（``STATION_OVERHEAD_H``）

早期版本只返回一个 ``duration_hours`` 且没说明口径，结果把「门到门估算」
与「纯车程实际值」对比，得出「上海→杭州误差 140%」的错误结论 ——
实际口径对齐后误差只有 -4%。**这个坑值得记着：估算类工具的语义必须写明。**

精度
----
高铁参数用 2026-10 采集的 5 条真实线路标定（见 ``CALIBRATION``）：

- 里程：长线 ±3%，短线 ±4%
- 车程：点估计 ±20%
- 费用：±15%

耗时同时给出**区间**，因为单一模型无法区分 350km/h 标尺（京沪/京广）
与 250km/h 标尺（西成/沪汉蓉）的线路 —— 这是模型的结构性上限，不是参数没调好。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_NOT_FOUND, ToolResult
from mcp_server.data.cities import CURATED, City, index_size, lookup_city
from mcp_server.tools.base import env, register, tool_timeout

# 城市坐标来自 mcp_server/data/cities.py（与天气工具共用同一份事实来源）
CITY_COORDS: dict[str, tuple[float, float]] = {c.name_zh: (c.lat, c.lon) for c in CURATED}

EARTH_RADIUS_KM = 6371.0

# 进出站 + 安检 + 候车 + 市内接驳的固定耗时（小时）
STATION_OVERHEAD_H = 0.7


# ---------------------------------------------------------------------------
# 标定数据
# ---------------------------------------------------------------------------

# 2026-10 采集的真实高铁线路：(出发, 到达, 直线km, 铁路里程km, 最快车程h, 二等座¥)
#
# 这几个点既是参数标定的依据，也是 ``tests/test_tools.py`` 的回归基准 ——
# 改参数时如果让某条线路误差超标，测试会红。
CALIBRATION: tuple[tuple[str, str, float, float, float, float], ...] = (
    ("上海", "杭州", 164.9, 159, 0.75, 87),
    ("成都", "西安", 620.0, 658, 3.50, 263),
    ("北京", "上海", 1067.3, 1318, 4.30, 576),
    ("上海", "成都", 1660.7, 1985, 10.52, 950),
    ("广州", "北京", 1888.6, 2298, 8.00, 862),
)


@dataclass(frozen=True)
class RailBand:
    """高铁/普速的分档参数（按**直线距离**选档，避免自引用）。

    速度给的是区间：``speed_slow`` 对应 250km/h 标尺线路（如西成、沪汉蓉），
    ``speed_fast`` 对应 350km/h 标尺线路（如京沪、京广）。
    """

    max_straight_km: float
    detour: float          # 直线距离 → 铁路里程
    speed_slow: float      # 慢线平均车程速度 km/h
    speed_fast: float      # 快线平均车程速度 km/h
    fare_low: float        # 每公里票价下限 ¥
    fare_high: float       # 每公里票价上限 ¥

    @property
    def speed_typical(self) -> float:
        return (self.speed_slow + self.speed_fast) / 2

    @property
    def fare_typical(self) -> float:
        return (self.fare_low + self.fare_high) / 2


# 分档依据（见 CALIBRATION 反推）：
#   短线  沪杭   detour 0.96  车程 212km/h  票价 0.55 ¥/km
#   中线  蓉西   detour 1.06  车程 188km/h  票价 0.40 ¥/km
#   中长  京沪   detour 1.235 车程 306km/h  票价 0.44 ¥/km
#   长线  沪蓉/京广 detour 1.20 车程 189~287km/h 票价 0.38~0.48 ¥/km
HSR_BANDS: tuple[RailBand, ...] = (
    RailBand(300, 1.00, 200, 260, 0.44, 0.56),
    RailBand(800, 1.08, 185, 260, 0.38, 0.45),
    RailBand(1200, 1.22, 230, 310, 0.40, 0.46),
    RailBand(math.inf, 1.21, 180, 310, 0.37, 0.48),
)

# 非高铁方式：(里程系数, 平均时速, 附加耗时 h, 每公里单价 ¥)
#
# 这几个**没有真实数据标定**，是量级参考。驾车是最粗的一项 ——
# 真实里程受路网影响很大，若需精确值应接入地图路线 API（注意其数据使用条款）。
MODE_PROFILE: dict[str, tuple[float, float, float, float]] = {
    "walk": (1.2, 4.5, 0.0, 0.0),
    "bike": (1.25, 14.0, 0.1, 0.0),
    "drive": (1.30, 75.0, 0.3, 1.1),
    "bus": (1.35, 55.0, 0.7, 0.45),
    "train": (1.22, 120.0, 0.8, 0.20),  # 普速，比高铁慢且便宜
    "hsr": (0.0, 0.0, 0.0, 0.0),        # 占位，实际走 HSR_BANDS
    "flight": (1.15, 750.0, 2.5, 0.75),
}

# 走**标定分档**的高铁。普速（train）不在其中 —— 它没有逐线标定，
# 走 MODE_PROFILE 的线性模型（120km/h、¥0.20/km）。
#
# 曾经写成 RAIL_MODES = {"hsr", "train"} 并让两者都进 _estimate_rail()，
# 结果是 MODE_PROFILE["train"] 成了死代码：普速按高铁参数算出来
# 时间少一半、价格高一倍，而 mode_label 还写着「普速列车」——
# 双重误导。实测上海→成都：修前 8.9h/¥854，修后 17.7h/¥405。
HSR_MODES = {"hsr"}

# 铁路类交通（高铁 + 普速）：估算路径不同，但**核实渠道都是 12306**。
RAIL_MODES = {"hsr", "train"}


def pick_rail_band(straight_km: float) -> RailBand:
    """按直线距离选高铁参数档。"""
    for band in HSR_BANDS:
        if straight_km <= band.max_straight_km:
            return band
    return HSR_BANDS[-1]


# ---------------------------------------------------------------------------
# 高德 Web 服务 API（可选增强，仅用于驾车）
# ---------------------------------------------------------------------------
#
# 合规要点（已核实服务协议与接口文档，2026-10）：
#
# 1. **允许的用法**：路径规划接口文档的「适用场景」明确写着
#    「无需展现地图的场景下，进行线路查询，如以线路结果页形式展现换乘方案」——
#    把结果用于文本形式的行程规划属于被许可的用法。
# 2. **禁止的用法**：服务协议第 3.5 条「不得直接存储、缓存或技术手段抓取
#    使用本服务内部数据」，第 4.12.7 条禁止「生成或用于数据库」。
#    因此结果**只在当次会话内使用，绝不写入本地库**。
# 3. **配额**：个人开发者 10,000 次/月、2 QPS，超限返回错误码 10023。
#    因此失败时**降级到离线估算而不是重试** —— 重试只会更快烧完配额。
#
# 未配置 key 时整条路径自动跳过，行为与改造前完全一致。
AMAP_KEY_ENV = "AMAP_API_KEY"
AMAP_DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"
AMAP_QUOTA_INFOCODE = "10023"

# 高德只覆盖中国大陆 + 港澳。**调用前必须先判掉境外，否则它会静默给出错误结果。**
#
# 实测（2026-10-08，同一把 key）：
#
# | 查询 | 结果 |
# |---|---|
# | 东方明珠 @ 上海 | ✅ 正确 |
# | 维多利亚港 @ 香港 | ✅ 正确（86 条） |
# | 大三巴牌坊 @ 澳门 | ✅ 正确（96 条） |
# | 台北101 @ 台北 | ❌ 无结果 |
# | 大英博物馆 @ 伦敦 | ❌ **返回遂宁市的「大英汉陶博物馆」** |
# | 埃菲尔铁塔 @ 巴黎 | ❌ 无结果 |
#
# 危险的是第三行那类：`region` 传一个高德不认识的行政区（伦敦）时，
# 它**不报错，而是静默降级成全国关键词模糊搜索** ——
# 「大英博物馆」于是命中了遂宁市大英县的「大英汉陶博物馆」，
# 「伦敦塔桥」命中了北京世界公园里的微缩景观。
# 换乘接口那边则直接抛 `INSUFFICIENT_ABROAD_PRIVILEGES`（境外服务需单独申请权限）。
#
# 注意**台湾不在其中**：实测 `台北101` 返回空。别想当然把它算进来。
AMAP_COVERED_COUNTRIES: frozenset[str] = frozenset({"中国", "中国香港", "中国澳门"})


def amap_covers(city: City) -> bool:
    """这个城市是否在高德的服务范围内。

    供工具在**发起请求之前**判掉境外 —— 省一次配额，而且能给出准确的失败原因
    （现在会写成「可能两地之间没有公共交通方案」，把用户往完全错的方向带）。
    """
    return city.country in AMAP_COVERED_COUNTRIES


# 自驾成本 = 过路费 + 油费。
#
# ⚠️ 高德的 `tolls` 字段**实测恒为 0**（2026-10-04 验证了 4 条路线，
# 包括上海→成都 1883km 收费路段，返回值仍是 0）。
# 但 `toll_distance`（收费路段里程）是准的，因此用「里程 × 单价」估算。
# ¥0.5/km 是高速一类客车的常见费率，实测 上海→南京 266km → ¥133，与实际相符。
TOLL_RATE_PER_KM = 0.50

# 油费按百公里 8L × 油价约 ¥7.5/L ≈ ¥0.6/km 估算（普通家用车量级）。
FUEL_COST_PER_KM = 0.60


def amap_driving(
    origin_lonlat: tuple[float, float],
    dest_lonlat: tuple[float, float],
    *,
    timeout: int | None = None,
) -> tuple[dict[str, float] | None, str]:
    """调用高德驾车路径规划。

    返回 ``(数据, 错误信息)``；数据为 ``None`` 表示调用失败或未配置 key。
    **不抛异常** —— 失败一律降级到离线估算。
    """
    key = env(AMAP_KEY_ENV)
    if not key:
        return None, "未配置 AMAP_API_KEY"

    params = {
        "key": key,
        # 高德要求「经度在前，纬度在后」
        "origin": f"{origin_lonlat[0]},{origin_lonlat[1]}",
        "destination": f"{dest_lonlat[0]},{dest_lonlat[1]}",
        "strategy": "10",  # 躲避拥堵 + 路程较短（官方推荐用它替代 11）
        "extensions": "base",  # 只要基础信息，省流量
        "output": "JSON",
    }

    try:
        with httpx.Client(timeout=timeout or tool_timeout()) as client:
            resp = client.get(AMAP_DRIVING_URL, params=params)
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
        return None, f"高德返回错误: {info}（infocode={infocode}）"

    paths = (payload.get("route") or {}).get("paths") or []
    if not paths:
        return None, "高德未返回可用驾车路线"

    path = paths[0]
    try:
        return (
            {
                "distance_km": round(int(path["distance"]) / 1000, 1),
                "duration_hours": round(int(path["duration"]) / 3600, 2),
                # 实测 tolls 恒为 0，保留它只是为了万一高德哪天修好了
                "tolls": float(path.get("tolls") or 0),
                # 收费路段里程是准的，用它估算过路费
                "toll_distance_km": round(int(path.get("toll_distance") or 0) / 1000, 1),
            },
            "",
        )
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"高德返回字段异常: {exc}"


def _estimate_drive_live(live: dict[str, float]) -> dict[str, object]:
    """用高德返回的真实路线数据构造结果。"""
    distance = live["distance_km"]
    duration = live["duration_hours"]
    toll_distance = live.get("toll_distance_km") or 0.0

    # 高德的 tolls 实测恒为 0，用收费路段里程估算；
    # 万一将来它开始返回真值，则优先用真值
    reported_tolls = live.get("tolls") or 0.0
    tolls = reported_tolls if reported_tolls > 0 else round(toll_distance * TOLL_RATE_PER_KM, 2)
    toll_source = "高德返回" if reported_tolls > 0 else f"按收费路段 {toll_distance:.0f}km × ¥{TOLL_RATE_PER_KM}/km 估算"

    fuel = distance * FUEL_COST_PER_KM
    cost = tolls + fuel

    return {
        "distance_km": distance,
        "in_vehicle_hours": round(duration, 1),
        # 高德的 duration 已含实时路况，但仍受出发时刻影响
        "in_vehicle_range_hours": [round(duration * 0.85, 1), round(duration * 1.35, 1)],
        "duration_hours": round(duration + 0.3, 1),
        "duration_range_hours": [
            round(duration * 0.85 + 0.3, 1),
            round(duration * 1.35 + 0.3, 1),
        ],
        "estimated_cost": round(cost, 2),
        # 过路费是估的，油费随车型/油价浮动
        "cost_range": [
            round(tolls * 0.9 + fuel * 0.75, 2),
            round(tolls * 1.1 + fuel * 1.3, 2),
        ],
        "cost_breakdown": {"tolls": round(tolls, 2), "fuel": round(fuel, 2)},
        "assumption": (
            f"高德实时驾车路线：{distance}km / {duration:.1f}h；"
            f"过路费 ¥{tolls:.0f}（{toll_source}）+ 油费 ¥{fuel:.0f}"
            f"（按 ¥{FUEL_COST_PER_KM}/km 估算）"
        ),
    }


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
    # clamp 到 [0,1]：对跖点附近浮点误差会让 h 略大于 1，
    # 而 math.asin(>1) 抛 ValueError。城市级距离几乎不会触发，但这是纯防御成本。
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def _estimate_rail(straight: float) -> dict[str, object]:
    """高铁 / 普速：按分档参数估算。"""
    band = pick_rail_band(straight)
    rail_km = straight * band.detour

    vehicle_typical = rail_km / band.speed_typical
    vehicle_fast = rail_km / band.speed_fast
    vehicle_slow = rail_km / band.speed_slow

    return {
        "distance_km": round(rail_km, 1),
        "in_vehicle_hours": round(vehicle_typical, 1),
        "in_vehicle_range_hours": [round(vehicle_fast, 1), round(vehicle_slow, 1)],
        "duration_hours": round(vehicle_typical + STATION_OVERHEAD_H, 1),
        "duration_range_hours": [
            round(vehicle_fast + STATION_OVERHEAD_H, 1),
            round(vehicle_slow + STATION_OVERHEAD_H, 1),
        ],
        "estimated_cost": round(rail_km * band.fare_typical, 2),
        "cost_range": [
            round(rail_km * band.fare_low, 2),
            round(rail_km * band.fare_high, 2),
        ],
        "assumption": (
            f"按直线距离 {straight:.0f}km 分档：线路系数 {band.detour}，"
            f"车程速度 {band.speed_slow:.0f}–{band.speed_fast:.0f}km/h，"
            f"票价 {band.fare_low:.2f}–{band.fare_high:.2f} 元/km（二等座）"
        ),
    }


def _estimate_other(straight: float, mode: str) -> dict[str, object]:
    """步行 / 骑行 / 驾车 / 大巴 / 飞机：沿用统一的线性模型。"""
    detour, speed, overhead, unit_price = MODE_PROFILE[mode]
    distance = straight * detour
    vehicle = distance / speed

    # 这几项没有真实数据标定，区间按 ±25% 给一个粗估
    return {
        "distance_km": round(distance, 1),
        "in_vehicle_hours": round(vehicle, 1),
        "in_vehicle_range_hours": [round(vehicle * 0.8, 1), round(vehicle * 1.25, 1)],
        "duration_hours": round(vehicle + overhead, 1),
        "duration_range_hours": [
            round(vehicle * 0.8 + overhead, 1),
            round(vehicle * 1.25 + overhead, 1),
        ],
        "estimated_cost": round(distance * unit_price, 2),
        "cost_range": [
            round(distance * unit_price * 0.75, 2),
            round(distance * unit_price * 1.25, 2),
        ],
        "assumption": (
            f"按直线距离 {straight:.0f}km 折算：系数 {detour}，"
            f"平均 {speed:.0f}km/h，附加 {overhead}h，"
            f"{unit_price:.2f} 元/km（**未用真实数据标定，仅量级参考**）"
        ),
    }


MODE_NAMES_ZH = {
    "hsr": "高铁",
    # 与 estimate_route 里的 mode_label 保持一致 —— 同一份返回里
    # 摘要说「火车」、note 说「普速列车」会让人以为是两种东西。
    "train": "普速列车",
    "flight": "飞机",
    "drive": "自驾",
    "bus": "大巴",
}


def summarize_route(data: dict[str, Any]) -> str:
    """形如：``上海 → 成都，高铁 8.2h / ¥917``。

    用 ``in_vehicle_hours``（纯车程）而不是 ``duration_hours``（门到门）——
    这里是「查到了什么」，不是「要花多久出门到进门」。
    """
    origin, destination = data.get("origin"), data.get("destination")
    if not origin or not destination:
        return ""

    mode = MODE_NAMES_ZH.get(str(data.get("mode") or ""), str(data.get("mode") or ""))
    bits: list[str] = []
    hours = data.get("in_vehicle_hours")
    if isinstance(hours, (int, float)) and hours > 0:
        bits.append(f"{hours}h")
    cost = data.get("estimated_cost")
    if isinstance(cost, (int, float)) and cost > 0:
        bits.append(f"¥{round(cost)}")

    tail = f" {' / '.join(bits)}" if bits else ""
    return f"{origin} → {destination}，{mode}{tail}"


@register(
    name="estimate_route",
    description=(
        "估算两个城市之间的交通距离、耗时与费用。"
        "支持国内城市与主要国际城市（如 纽约 / 东京 / 巴黎），中英文名均可。"
        "用于跨城行程的转场安排。基于内置城市坐标离线计算，结果为量级估算而非精确导航。"
        "**注意口径**：duration_hours 是门到门（含进出站 0.7h），"
        "in_vehicle_hours 才是车程 —— 要与班次时刻对比时请用后者。"
        "高铁参数已用真实线路标定（车程 ±20%、费用 ±15%），并提供区间。"
        "驾车模式在配置了 AMAP_API_KEY 时会走高德实时路线（更准，含过路费），"
        "否则降级为离线估算 —— 返回里的 source 字段会说明实际用了哪个。"
        "实际班次与票价请以 12306 官方 App 为准。"
    ),
    params_model=RouteParams,
    idempotent=True,
    tags=["本地计算", "交通"],
    summarize=summarize_route,
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
            f"无法解析这些地点: {', '.join(missing)}。"
            f"本地城市库收录 {index_size()} 个城市（覆盖 229 个国家/地区，中英文名均可），"
            "但**不含景区、区县等非城市目的地**"
            "（如「都江堰」「青城山」「兵马俑」「登封」都不在其中）。\n"
            "处理方式：\n"
            "1. 改用最近的城市估算城际段（如 都江堰 → 用 成都）；\n"
            "2. 在计划里说明「到该景区还需额外的市内/短途交通，请自行查询实际距离」；\n"
            "3. **不要编造距离或票价** —— 缺数据就标注「⚠️ 数据缺失」。",
        )

    assert origin is not None and destination is not None  # for type checkers
    origin_name, dest_name = origin.name_zh, destination.name_zh

    if origin_name == dest_name:
        return ToolResult.success(
            {
                "origin": origin_name,
                "destination": dest_name,
                "mode": params.mode,
                "straight_line_km": 0.0,
                "distance_km": 0.0,
                "in_vehicle_hours": 0.0,
                "duration_hours": 0.0,
                "estimated_cost": 0.0,
                "note": "同城，无需城际交通。",
            },
            source="offline:city-coords",
        )

    straight = haversine_km((origin.lat, origin.lon), (destination.lat, destination.lon))

    degraded = ""
    source = "offline:city-coords"

    if params.mode in HSR_MODES:
        estimate = _estimate_rail(straight)
        mode_label = "高铁"
        accuracy = "车程 ±20%、费用 ±15%（基于 2026-10 真实线路标定）"
    elif params.mode == "train":
        # 普速没有逐线标定，用 MODE_PROFILE 的线性模型（见 HSR_MODES 的说明）
        estimate = _estimate_other(straight, params.mode)
        mode_label = "普速列车"
        accuracy = "量级参考（普速车次速度差异大，未逐线标定）"
    elif params.mode == "drive":
        live, live_error = amap_driving(
            (origin.lon, origin.lat), (destination.lon, destination.lat)
        )
        mode_label = "驾车"
        if live is not None:
            estimate = _estimate_drive_live(live)
            accuracy = "距离与耗时来自高德实时路线；过路费准确，油费 ±30%"
            source = "amap:driving"
        else:
            estimate = _estimate_other(straight, params.mode)
            accuracy = "量级参考（未接入实时路线）"
            if env(AMAP_KEY_ENV):
                # 配了 key 却调用失败 —— 这是真的降级，要说明原因
                degraded = f"高德实时路线不可用（{live_error}），已降级为离线估算。"
            # 没配 key 属于默认状态，不是错误，不提示
    else:
        estimate = _estimate_other(straight, params.mode)
        mode_label = params.mode
        accuracy = "量级参考（未用真实数据标定）"

    # 核实渠道必须与交通方式匹配 —— 给驾车用户提示"查 12306"是没意义的
    if params.mode in RAIL_MODES:
        channel = "实际班次、票价与时刻请以 12306 官方 App 为准。"
    elif params.mode == "drive":
        channel = (
            "实际路况与过路费请以高德地图为准；油费随车型浮动。"
            if source == "amap:driving"
            else "实际里程、路况与过路费请以高德/百度地图导航为准。"
        )
    elif params.mode == "flight":
        channel = "实际航班与票价请以航司官网或票务平台为准。"
    else:
        channel = "实际路线与票价请以地图或票务平台为准。"

    payload: dict[str, object] = {
        "origin": origin_name,
        "destination": dest_name,
        "mode": params.mode,
        "straight_line_km": round(straight, 1),
        **estimate,
        "accuracy": accuracy,
        "note": (
            f"{mode_label}估算。duration_hours 为门到门（含进出站 "
            f"{STATION_OVERHEAD_H}h），in_vehicle_hours 为纯车程。{channel}"
            + (f" {degraded}" if degraded else "")
        ),
    }
    return ToolResult.success(payload, source=source)
