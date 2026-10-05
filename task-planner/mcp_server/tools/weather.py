"""工具 T1：get_weather_forecast —— 对接 Open-Meteo（免 API Key）。

城市解析策略（重要）
--------------------
**本地坐标表优先，未命中才回落 Open-Meteo 地理编码。**

原因是 Open-Meteo 的地理编码对中文城市名基本不可用，且会静默给出错误结果：

    name=纽约     -> 无结果
    name=东京     -> 命中「江苏·东京」(32.21,119.29)
    name=New York -> 命中「内布拉斯加州·约克」

第二种情况最危险 —— 不报错，直接把江苏的天气当成东京天气返回，
正好违反「事实数据不得臆造」这条硬约束。

回落时我们会拉多个候选、按行政级别与人口排序，并把
`resolved_name / country / admin1 / population` 一并返回，供模型复核。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_BAD_ARGS, ERR_NOT_FOUND, ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.data.cities import index_size, lookup_city
from mcp_server.tools.base import register, tool_timeout

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo 越界时报 "out of allowed range from YYYY-MM-DD to YYYY-MM-DD"
ALLOWED_RANGE_RE = re.compile(
    r"from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})"
)

# WMO 天气代码 -> 中文描述（只保留常见档位）
WMO_CODES: dict[int, str] = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

# GeoNames 行政级别：PPLC=首都，PPLA=一级行政区首府，PPL=普通居民点
FEATURE_PRIORITY: dict[str, int] = {
    "PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5,
}


class WeatherParams(BaseModel):
    city: str = Field(description="城市名，如 成都 / 纽约 / Tokyo")
    start_date: str = Field(description="开始日期，格式 YYYY-MM-DD")
    end_date: str = Field(description="结束日期，格式 YYYY-MM-DD")

    @field_validator("city")
    @classmethod
    def _city_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("city 不能为空")
        return v.strip()

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(f"日期格式必须是 YYYY-MM-DD，收到 {v!r}") from exc
        return v


def _fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """薄封装：便于测试注入假响应。"""
    timeout = tool_timeout()
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# 城市解析
# ---------------------------------------------------------------------------


class ResolvedCity(BaseModel):
    """城市解析结果，含足够元信息供模型复核。"""

    query: str
    latitude: float
    longitude: float
    timezone: str
    resolved_name: str
    country: str = ""
    admin1: str = ""
    population: int | None = None
    matched_via: str  # local-city-table | open-meteo-geocoding
    confidence: str = "high"  # high | low
    note: str = ""


def _rank_candidate(candidate: dict[str, Any]) -> tuple[int, int]:
    """越小越优：先看行政级别，再看人口。"""
    priority = FEATURE_PRIORITY.get(candidate.get("feature_code", ""), 9)
    population = candidate.get("population") or 0
    return priority, -population


def resolve_city(query: str) -> tuple[ResolvedCity | None, str]:
    """解析城市。返回 (结果, 错误信息)。

    顺序：策展表 → GeoNames 生成索引 → Open-Meteo 地理编码（带置信度标记）。
    """
    # 1) 本地解析（策展表 + 生成索引，共 1.4 万+ 城市）
    local = lookup_city(query)
    if local is not None:
        return (
            ResolvedCity(
                query=query,
                latitude=local.lat,
                longitude=local.lon,
                timezone=local.timezone,
                resolved_name=local.name_zh,
                country=local.country,
                matched_via=local.source,  # curated-table | city-index
            ),
            "",
        )

    # 2) 回落 Open-Meteo 地理编码
    geo = _fetch_json(
        GEOCODE_URL,
        {"name": query, "count": 10, "language": "zh", "format": "json"},
    )
    results = geo.get("results") or []
    if not results:
        return None, (
            f"未找到城市「{query}」。本地城市库已收录 {index_size()} 个城市"
            "（覆盖 229 个国家/地区，中英文名均可）。"
            "请确认拼写；若确实是本地库未收录的小众目的地，"
            "请如实告知用户「无法获取该地天气」，不要凭记忆编造。"
        )

    best = sorted(results, key=_rank_candidate)[0]
    priority, _ = _rank_candidate(best)

    # 行政级别低 + 人口少 => 大概率不是用户想去的那个城市，标记为低置信
    low_confidence = priority >= 5 and (best.get("population") or 0) < 50_000
    note = ""
    if low_confidence:
        note = (
            f"⚠️ 地理编码只匹配到一个低置信度结果（{best.get('name')} / "
            f"{best.get('admin1', '')} / {best.get('country', '')}）。"
            "请向用户确认这就是目标城市，不要直接当成事实采信。"
        )

    return (
        ResolvedCity(
            query=query,
            latitude=best["latitude"],
            longitude=best["longitude"],
            timezone=best.get("timezone") or "Asia/Shanghai",
            resolved_name=best.get("name", query),
            country=best.get("country", ""),
            admin1=best.get("admin1", ""),
            population=best.get("population"),
            matched_via="open-meteo-geocoding",
            confidence="low" if low_confidence else "high",
            note=note,
        ),
        "",
    )


# ---------------------------------------------------------------------------
# 建议生成
# ---------------------------------------------------------------------------


def _advice(days: list[dict[str, Any]]) -> str:
    if not days:
        return "无有效预报数据。"
    rainy = [d for d in days if (d["precip_prob"] or 0) >= 50]
    temps = [t for d in days for t in (d["tmax"], d["tmin"]) if t is not None]
    hottest = max(temps) if temps else None
    coldest = min(temps) if temps else None

    parts: list[str] = []
    if rainy:
        parts.append(f"{len(rainy)}/{len(days)} 天降水概率≥50%，建议安排室内备选方案")
    else:
        parts.append("降水概率低，适合户外活动")
    if hottest is not None and hottest >= 33:
        parts.append(f"最高 {hottest}℃，注意防晒补水")
    if coldest is not None and coldest <= 5:
        parts.append(f"最低 {coldest}℃，注意保暖")
    return "；".join(parts) + "。"


def summarize_weather(data: dict[str, Any]) -> str:
    """把预报压成一句人话，供前端「已查证」列表展示。

    形如：``10-03 起 3 天，多云 18–26°C · 10-04 有小雨``
    """
    days = data.get("daily") or []
    if not days:
        return ""

    first = days[0]
    start = str(first.get("date") or "")[5:]  # YYYY-MM-DD → MM-DD
    lows = [d["tmin"] for d in days if isinstance(d.get("tmin"), (int, float))]
    highs = [d["tmax"] for d in days if isinstance(d.get("tmax"), (int, float))]

    bits = [f"{start} 起 {len(days)} 天"]
    weather = str(first.get("weather") or "").strip()
    if weather and weather != "未知":
        bits.append(weather)
    if lows and highs:
        bits.append(f"{round(min(lows))}–{round(max(highs))}°C")

    out = "，".join(bits)
    rainy = [
        str(d.get("date") or "")[5:]
        for d in days
        if isinstance(d.get("precip_prob"), (int, float)) and d["precip_prob"] >= 50
    ]
    if rainy:
        out += f" · {'、'.join(rainy)} 有小雨"
    return out


@register(
    name="get_weather_forecast",
    description=(
        "获取指定城市在给定日期区间的每日天气预报（最高/最低温、降水概率、天气状况）。"
        "支持中文名（成都 / 纽约 / 东京）与英文名（New York / Tokyo）。"
        "当计划涉及户外活动或需要选择出行日期时必须调用，禁止凭记忆编造天气。"
    ),
    params_model=WeatherParams,
    idempotent=True,
    tags=["外部API", "天气"],
    summarize=summarize_weather,
)
def get_weather_forecast(params: WeatherParams) -> ToolResult:
    # 1) 解析城市
    city, error = resolve_city(params.city)
    if city is None:
        return ToolResult.failure(ERR_NOT_FOUND, error)

    # 2) 拉每日预报
    try:
        forecast = _fetch_json(
            FORECAST_URL,
            {
                "latitude": city.latitude,
                "longitude": city.longitude,
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,weathercode"
                ),
                "timezone": city.timezone,
                "start_date": params.start_date,
                "end_date": params.end_date,
            },
        )
    except httpx.HTTPStatusError as exc:
        # 不要把上游的错误原因吞掉 —— 否则用户只看到「400」不知道该怎么办
        reason = ""
        try:
            reason = exc.response.json().get("reason", "") or ""
        except Exception:  # noqa: BLE001 - 非 JSON 响应
            reason = (exc.response.text or "")[:200]

        match = ALLOWED_RANGE_RE.search(reason)
        if match:
            return ToolResult.failure(
                ERR_BAD_ARGS,
                f"请求的日期超出预报范围：{params.start_date} ~ {params.end_date}。"
                f"Open-Meteo 当前只提供 {match.group(1)} ~ {match.group(2)} 的预报，"
                "请改用该区间内的日期；更远的日期只能给出气候意义上的经验判断，"
                "并需明确标注为「非实时预报」。",
            )
        return ToolResult.failure(
            ERR_UPSTREAM_ERROR,
            f"预报接口返回 {exc.response.status_code}"
            + (f"：{reason}" if reason else ""),
        )

    daily = forecast.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        return ToolResult.failure(
            ERR_NOT_FOUND,
            f"该日期区间无预报（Open-Meteo 通常只提供未来约 16 天）: "
            f"{params.start_date} ~ {params.end_date}",
        )

    days = [
        {
            "date": d,
            "tmax": (daily.get("temperature_2m_max") or [None] * len(dates))[i],
            "tmin": (daily.get("temperature_2m_min") or [None] * len(dates))[i],
            "precip_prob": (daily.get("precipitation_probability_max") or [None] * len(dates))[i],
            "weather": WMO_CODES.get(
                (daily.get("weathercode") or [None] * len(dates))[i], "未知"
            ),
        }
        for i, d in enumerate(dates)
    ]

    warnings = [city.note] if city.note else []

    return ToolResult.success(
        {
            "city": params.city,
            # 解析出的真实位置：模型应当核对这里再决定是否采信
            "resolved": {
                "name": city.resolved_name,
                "country": city.country,
                "admin1": city.admin1,
                "latitude": city.latitude,
                "longitude": city.longitude,
                "timezone": city.timezone,
                "matched_via": city.matched_via,
                "confidence": city.confidence,
            },
            "daily": days,
            "advice": _advice(days),
            "warnings": warnings,
        },
        source="open-meteo.com",
    )
