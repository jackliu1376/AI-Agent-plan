"""工具 T9：query_transit_options —— 实时公交 / 铁路换乘方案（高德）。

与 ``estimate_route`` 的分工
----------------------------
- ``estimate_route`` —— **估算**。离线、零依赖、任何城市对都能用，
  但结果是量级参考（高铁车程 ±20%）。
- ``query_transit_options`` —— **实时班次**。返回真实车次、发到时刻与票价，
  但**需要 AMAP_API_KEY**，且只覆盖中国境内。

需要「具体坐哪趟车、几点发、多少钱」时用本工具；
只需要「大概多久、大概多少钱」时用 ``estimate_route``。

⚠️ 成功路径未实测
------------------
开发环境没有可用的高德 key，**只验证了错误路径**
（``{"status":"0","info":"INVALID_USER_KEY","infocode":"10001"}``，
与驾车接口格式一致）。成功路径的解析按官方文档实现，
并用文档结构造的 fixture 做了单元测试 —— 但这**不等于**线上可用。

拿到 key 后请先跑探针确认字段：

    uv run python -m mcp_server.tools.transit --probe 上海 成都

合规边界（与驾车接口相同）
--------------------------
- ✅ 接口文档的「适用场景」明确含「无需展现地图的场景」，用于文本行程规划是被允许的
- ❌ 服务协议第 3.5 条禁止存储/缓存 —— 结果只在当次会话内使用，**不落库**
- ⚠️ 个人开发者配额 10,000 次/月、2 QPS，超限返回 10023；
  失败时**降级而不是重试**，重试只会更快烧完配额
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_BAD_ARGS, ERR_NOT_FOUND, ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.data.cities import lookup_city
from mcp_server.tools.base import env, register, tool_timeout
from mcp_server.tools.route import AMAP_KEY_ENV, AMAP_QUOTA_INFOCODE, amap_covers

AMAP_TRANSIT_URL = "https://restapi.amap.com/v3/direction/transit/integrated"

# 火车线路类型码 → 人话（来自官方文档的「火车路线类型表」）
RAIL_TYPE_NAMES: dict[str, str] = {
    "2010": "普客",
    "2011": "高铁 G",
    "2012": "动车 D",
    "2013": "城际 C",
    "2014": "直达特快 Z",
    "2015": "特快 T",
    "2016": "快车 K",
    "2017": "临客 L/Y",
    "2018": "市郊 S",
}

# 仓位编码 → 人话（来自官方文档的「仓位级别表」，只保留铁路相关）
SEAT_CLASS_NAMES: dict[str, str] = {
    "0": "不分仓位",
    "9": "特等座",
    "10": "硬座",
    "11": "软座",
    "12": "一等座",
    "13": "二等座",
    "14": "硬卧上铺",
    "15": "硬卧中铺",
    "16": "硬卧下铺",
    "17": "软卧上铺",
    "18": "软卧下铺",
    "19": "高级软卧上铺",
    "20": "高级软卧下铺",
    "21": "商务座",
}

# 公交换乘策略（来自官方文档）
STRATEGY_NAMES = {
    "0": "最快捷",
    "1": "最经济",
    "2": "最少换乘",
    "3": "最少步行",
    "5": "不乘地铁",
}

MAX_OPTIONS = 5


class TransitParams(BaseModel):
    origin: str = Field(description="出发城市，如 上海")
    destination: str = Field(description="目的城市，如 成都")
    strategy: str = Field(
        default="0",
        description="换乘策略：0=最快捷 1=最经济 2=最少换乘 3=最少步行 5=不乘地铁",
    )
    date: str | None = Field(default=None, description="出发日期 YYYY-MM-DD，可选")
    time: str | None = Field(default=None, description="出发时间 HH:MM，可选")
    max_options: int = Field(default=3, ge=1, le=MAX_OPTIONS, description="返回方案数上限")

    @field_validator("origin", "destination")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("城市名不能为空")
        return v.strip()

    @field_validator("strategy")
    @classmethod
    def _check_strategy(cls, v: str) -> str:
        v = v.strip()
        if v not in STRATEGY_NAMES:
            raise ValueError(f"不支持的策略 {v!r}，可选: {', '.join(STRATEGY_NAMES)}")
        return v


# ---------------------------------------------------------------------------
# 解析（全部防御式：字段缺失就跳过，不让整个工具失败）
# ---------------------------------------------------------------------------


def _minutes_since_midnight(raw: Any) -> int | None:
    """把 ``"0730"`` / ``"2405"`` 这类时刻转成「距 0 点的分钟数」。

    高德约定：大于 24:00 表示跨天，因此不取模。
    """
    text = str(raw or "").strip()
    if len(text) < 3 or not text.isdigit():
        return None
    text = text.zfill(4)
    return int(text[:2]) * 60 + int(text[2:4])


def _fmt_clock_at(minutes: int, *, day_offset: int = 0) -> str:
    """把「距 0 点的分钟数」格式化成时刻，跨天时标出偏移。

    跨天必须标出来 —— 否则用户看到「20:45 发车，05:49 到达」会以为是当天早上到。
    """
    day, minute = divmod(minutes, 24 * 60)
    total_days = day + day_offset
    clock = f"{minute // 60:02d}:{minute % 60:02d}"
    if total_days <= 0:
        return clock
    if total_days == 1:
        return f"次日 {clock}"
    return f"{total_days} 天后 {clock}"


def _fmt_clock(raw: Any) -> str:
    """``"0730"`` → ``"07:30"``；``"2405"`` → ``"次日 00:05"``。"""
    minutes = _minutes_since_midnight(raw)
    return _fmt_clock_at(minutes) if minutes is not None else ""


def parse_railway(rail: dict[str, Any]) -> dict[str, Any] | None:
    """解析一段铁路行程。字段不全时返回 None（跳过而不是崩）。

    ⚠️ 两处**实测与文档不符**（2026-10-04 用真实 Key 探测发现）：

    1. ``type`` 返回的是中文描述（``"K字头的快车火车"``），
       不是文档里的数字码（``"2016"``）。两种都兼容。
    2. **跨天不用 >24:00**。实测 K282 上海 20:45 → 成都西 05:49，
       ``arrival_stop.time`` 就是普通的 ``"0549"``。
       因此**不能**简单相减（会得负数），**优先用 ``time`` 字段**（秒）。

    另有一处细节：``time``（秒）与真实时刻差**可能差 1 分钟**
    （D986 实测 48960s vs 18:44→08:21 的 49020s）。
    因此**耗时取 ``time``，到达时刻取真实时钟** —— 各用各的准的那个。
    """
    departure = rail.get("departure_stop") or {}
    arrival = rail.get("arrival_stop") or {}
    trip = str(rail.get("trip") or rail.get("name") or "").strip()
    if not trip and not departure.get("name"):
        return None

    depart_min = _minutes_since_midnight(departure.get("time"))
    arrive_raw = _minutes_since_midnight(arrival.get("time"))
    raw_seconds = str(rail.get("time") or "").strip()

    duration_hours = 0.0
    day_offset = 0

    if raw_seconds.isdigit():
        # 主路径：time 字段是秒，实测最可靠
        seconds = int(raw_seconds)
        duration_hours = round(seconds / 3600, 1)
        if depart_min is not None:
            day_offset = (depart_min + seconds // 60) // (24 * 60)
    elif depart_min is not None and arrive_raw is not None:
        # 回退：时刻相减。到达早于发车 → 按次日处理（实测就是这么返回的）
        diff = arrive_raw - depart_min
        if diff < 0:
            diff += 24 * 60
            day_offset = 1
        duration_hours = round(diff / 60, 1)

    if arrive_raw is not None:
        # 到达时刻有两种格式，必须区分（否则会**重复计算天数**）：
        #   A) 文档格式：>24:00 本身已含天数（"3230" = 次日 08:30）→ 直接用
        #   B) 实测格式：普通时刻（"0549"）→ 补上推算出的天数
        effective_offset = 0 if arrive_raw >= 24 * 60 else day_offset
        arrive_text = _fmt_clock_at(arrive_raw, day_offset=effective_offset)
    elif depart_min is not None and duration_hours:
        arrive_text = _fmt_clock_at(depart_min + int(duration_hours * 60))
    else:
        arrive_text = ""

    raw_type = str(rail.get("type") or "").strip()
    leg: dict[str, Any] = {
        "kind": "train",
        "trip": trip,
        # 实测返回中文描述，文档里的数字码已不用 —— 两种都兼容
        "type": RAIL_TYPE_NAMES.get(raw_type, raw_type),
        "from": departure.get("name") or "",
        "to": arrival.get("name") or "",
        "depart": _fmt_clock(departure.get("time")),
        "arrive": arrive_text,
        "duration_hours": duration_hours,
        "distance_km": round(int(rail.get("distance") or 0) / 1000, 1),
    }

    # 仓位价格（extensions=all 时才返回，且**实测常为空数组**）
    seats: list[dict[str, Any]] = []
    for alter in rail.get("alters") or []:
        for space in (alter.get("spaces") or []):
            code = str(space.get("code") or "")
            cost = space.get("cost")
            if cost is None:
                continue
            seats.append(
                {
                    "class": SEAT_CLASS_NAMES.get(code, f"仓位{code}"),
                    "price": float(cost),
                }
            )
    if seats:
        # 去重后按价格排序，最便宜的在前
        unique: dict[str, float] = {}
        for seat in seats:
            unique.setdefault(seat["class"], seat["price"])
        leg["seat_prices"] = [
            {"class": name, "price": price}
            for name, price in sorted(unique.items(), key=lambda kv: kv[1])
        ]

    return leg


def parse_transit_option(transit: dict[str, Any]) -> dict[str, Any] | None:
    """解析一个换乘方案。没有任何可用路段时返回 None。"""
    legs: list[dict[str, Any]] = []
    for segment in transit.get("segments") or []:
        rail = segment.get("railway")
        if isinstance(rail, dict):
            leg = parse_railway(rail)
            if leg:
                legs.append(leg)
                continue
        bus = segment.get("bus")
        if isinstance(bus, dict):
            for line in bus.get("buslines") or []:
                name = str(line.get("name") or "").strip()
                if name:
                    legs.append(
                        {
                            "kind": "bus",
                            "line": name,
                            "type": str(line.get("type") or ""),
                            "from": (line.get("departure_stop") or {}).get("name") or "",
                            "to": (line.get("arrival_stop") or {}).get("name") or "",
                        }
                    )

    if not legs:
        return None

    duration_s = int(transit.get("duration") or 0)
    cost = transit.get("cost")
    return {
        "duration_hours": round(duration_s / 3600, 1) if duration_s else 0.0,
        "cost": float(cost) if cost is not None else None,
        "walking_distance_m": int(transit.get("walking_distance") or 0),
        "is_night": str(transit.get("nightflag") or "0") == "1",
        "legs": legs,
    }


def parse_transit_payload(payload: dict[str, Any], *, limit: int = MAX_OPTIONS) -> list[dict[str, Any]]:
    """从高德响应里抽出换乘方案列表。"""
    transits = (payload.get("route") or {}).get("transits") or []
    options: list[dict[str, Any]] = []
    for transit in transits:
        if not isinstance(transit, dict):
            continue
        parsed = parse_transit_option(transit)
        if parsed is not None:
            options.append(parsed)
        if len(options) >= limit:
            break
    return options


def _summarize(options: list[dict[str, Any]]) -> dict[str, Any]:
    """给模型一份「最优方案」的摘要，避免它自己从列表里挑。"""
    # 优先挑有票价的（能算预算）；没有票价的按耗时排序
    priced = [o for o in options if o.get("cost")]
    ranked = sorted(priced or options, key=lambda o: (o.get("cost") or 0, o.get("duration_hours") or 0))
    best = ranked[0]
    trains = [leg for o in options for leg in o["legs"] if leg["kind"] == "train"]

    return {
        "option_count": len(options),
        "fastest_hours": min((o["duration_hours"] for o in options if o["duration_hours"]), default=0.0),
        "cheapest_cost": min((o["cost"] for o in priced), default=None),
        "train_numbers": [leg["trip"] for leg in trains if leg.get("trip")][:8],
        "recommended": {
            "duration_hours": best["duration_hours"],
            "cost": best.get("cost"),
            "trains": [leg["trip"] for leg in best["legs"] if leg["kind"] == "train"],
        },
    }


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def _fetch_transit(params: TransitParams, origin: Any, destination: Any) -> tuple[dict | None, str]:
    """调用高德公交路径规划。返回 ``(payload, 错误信息)``。"""
    key = env(AMAP_KEY_ENV)
    if not key:
        return None, "未配置 AMAP_API_KEY"

    query: dict[str, Any] = {
        "key": key,
        # 高德要求「经度在前，纬度在后」
        "origin": f"{origin.lon},{origin.lat}",
        "destination": f"{destination.lon},{destination.lat}",
        "city": origin.name_zh,
        "cityd": destination.name_zh,
        "strategy": params.strategy,
        "extensions": "all",  # 需要 all 才有仓位价格（spaces）
        "output": "JSON",
    }
    if params.date:
        query["date"] = params.date
    if params.time:
        query["time"] = params.time

    try:
        with httpx.Client(timeout=tool_timeout()) as client:
            resp = client.get(AMAP_TRANSIT_URL, params=query)
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


def summarize_transit(data: dict[str, Any]) -> str:
    """形如：``上海 → 成都，最快 8.2h，最低 ¥917，6 个方案``。"""
    origin, destination = data.get("origin"), data.get("destination")
    if not origin or not destination:
        return ""

    summary = data.get("summary") or {}
    bits: list[str] = []
    fastest = summary.get("fastest_hours")
    if isinstance(fastest, (int, float)) and fastest > 0:
        bits.append(f"最快 {fastest}h")
    cheapest = summary.get("cheapest_cost")
    if isinstance(cheapest, (int, float)) and cheapest > 0:
        bits.append(f"最低 ¥{round(cheapest)}")
    count = summary.get("option_count")
    if isinstance(count, int) and count > 0:
        bits.append(f"{count} 个方案")

    return f"{origin} → {destination}" + (f"，{'，'.join(bits)}" if bits else "")


@register(
    name="query_transit_options",
    description=(
        "查询两个中国城市之间的**实时公交换乘方案**（含高铁/动车车次、发到时刻与票价）。"
        "需要配置 AMAP_API_KEY，只覆盖中国境内。"
        "适合回答「坐哪趟车、几点发、多少钱」这类问题；"
        "若只需量级估算（或目的地在中国境外），用 estimate_route 更合适。"
        "返回里的 legs[].seat_prices 是各仓位票价（高德仅在部分线路返回）。"
        "**票价与余票请以 12306 官方 App 为准** —— 本工具的价格来自高德，可能有延迟。"
    ),
    params_model=TransitParams,
    idempotent=True,
    tags=["外部API", "交通"],
    summarize=summarize_transit,
)
def query_transit_options(params: TransitParams) -> ToolResult:
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
            f"无法解析这些城市: {', '.join(missing)}。"
            "本工具只覆盖中国境内城市，且不含景区/区县等非城市目的地。",
        )

    assert origin is not None and destination is not None

    # **调用前判掉境外。** 不判的话高德会返回 `INSUFFICIENT_ABROAD_PRIVILEGES`
    # （境外服务需单独申请权限），而旧代码把它包成一句
    # 「可能两地之间没有公共交通方案」—— 把用户往完全错的方向带：
    # 上海→伦敦当然没有铁路方案，但原因不是「没有」，是「查不了」。
    outside = [c for c in (origin, destination) if not amap_covers(c)]
    if outside:
        names = "、".join(f"{c.name_zh}（{c.country}）" for c in outside)
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"本工具（高德换乘规划）只覆盖中国大陆与港澳，"
            f"而 {names} 不在覆盖范围内。\n"
            "**这是覆盖范围问题，不是「两地之间没有交通」** —— "
            "不要据此告诉用户「没有可用的交通方式」。\n"
            "请改用 `estimate_route`：它按直线距离与分档参数做量级估算，"
            "**支持国际航线**（如 上海 → 伦敦 飞机会给出 14h / ¥7900 量级）。",
            tool="query_transit_options",
        )

    payload, error = _fetch_transit(params, origin, destination)
    if payload is None:
        # 不重试：配额有限，重试只会更快烧完
        return ToolResult.failure(ERR_UPSTREAM_ERROR, error, tool="query_transit_options")

    options = parse_transit_payload(payload, limit=params.max_options)
    if not options:
        return ToolResult.failure(
            ERR_UPSTREAM_ERROR,
            f"高德未返回可用的换乘方案（{origin.name_zh} → {destination.name_zh}）。"
            "可能两地之间确实没有公共交通方案（如小城市之间的短途）。"
            "请改用 estimate_route 做量级估算，或如实告知用户。",
            tool="query_transit_options",
        )

    return ToolResult.success(
        {
            "origin": origin.name_zh,
            "destination": destination.name_zh,
            "strategy": STRATEGY_NAMES[params.strategy],
            "summary": _summarize(options),
            "options": options,
            "note": (
                "方案与票价来自高德实时查询，可能有延迟或缺失；"
                "**实际车次、票价与余票请以 12306 官方 App 为准**。"
                "同一任务内重复查询会被缓存，不额外消耗配额。"
            ),
        },
        source="amap:transit",
    )


# ---------------------------------------------------------------------------
# 探针：拿到 key 后先跑这个确认字段结构
# ---------------------------------------------------------------------------


def _probe(origin: str, destination: str) -> int:
    """打印高德原始响应，用于验证解析器是否与实际字段匹配。

    开发环境没有可用 key，成功路径未实测 —— **拿到 key 后请先跑这个**。
    """
    key = env(AMAP_KEY_ENV)
    if not key:
        print("❌ 未配置 AMAP_API_KEY", file=sys.stderr)
        return 2

    o = lookup_city(origin)
    d = lookup_city(destination)
    if o is None or d is None:
        print(f"❌ 无法解析城市: {origin} / {destination}", file=sys.stderr)
        return 2

    query = {
        "key": key,
        "origin": f"{o.lon},{o.lat}",
        "destination": f"{d.lon},{d.lat}",
        "city": o.name_zh,
        "cityd": d.name_zh,
        "strategy": "0",
        "extensions": "all",
        "output": "JSON",
    }

    print(f"🔍 探测 {o.name_zh} → {d.name_zh}")
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.get(AMAP_TRANSIT_URL, params=query)
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if payload.get("status") != "1":
        print(f"❌ 高德返回错误: {payload.get('info')}（infocode={payload.get('infocode')}）")
        return 1

    transits = (payload.get("route") or {}).get("transits") or []
    print(f"✅ 返回 {len(transits)} 个方案")
    if transits:
        first = transits[0]
        print(f"\n方案一顶层字段: {sorted(first.keys())}")
        segments = first.get("segments") or []
        print(f"路段数: {len(segments)}")
        for i, seg in enumerate(segments):
            print(f"  路段 {i} 字段: {sorted(seg.keys())}")
            rail = seg.get("railway")
            if isinstance(rail, dict):
                print(f"    railway 字段: {sorted(rail.keys())}")
                print(f"    railway 样例: {json.dumps(rail, ensure_ascii=False)[:400]}")

    parsed = parse_transit_payload(payload, limit=2)
    print(f"\n解析结果: {len(parsed)} 个方案")
    print(json.dumps(parsed, ensure_ascii=False, indent=2)[:1200])
    return 0 if parsed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="高德公交路径规划探针")
    parser.add_argument("--probe", action="store_true", help="探测原始响应结构")
    parser.add_argument("origin", nargs="?", default="上海")
    parser.add_argument("destination", nargs="?", default="成都")
    args = parser.parse_args(argv)

    if not args.probe:
        parser.print_help()
        return 0
    return _probe(args.origin, args.destination)


if __name__ == "__main__":
    sys.exit(main())
