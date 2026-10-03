"""工具 T5：convert_currency —— 汇率换算。

优先查在线汇率；网络不可用或超时则退化为内置离线汇率表，
保证「工具失败不中断整体输出」这条约束在汇率上也成立。
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, field_validator

from common.envelope import ERR_BAD_ARGS, ToolResult
from mcp_server.tools.base import env, register, tool_timeout

# 离线兜底汇率（以 CNY 为基准，1 单位外币 = ? CNY）—— 教学用近似值
FALLBACK_TO_CNY: dict[str, float] = {
    "CNY": 1.0,
    "RMB": 1.0,
    "USD": 7.15,
    "EUR": 7.75,
    "JPY": 0.0475,
    "KRW": 0.0052,
    "HKD": 0.915,
    "TWD": 0.222,
    "MOP": 0.889,
    "GBP": 9.05,
    "THB": 0.205,
    "SGD": 5.35,
    "MYR": 1.62,
    "AUD": 4.70,
}


class CurrencyParams(BaseModel):
    amount: float = Field(description="待换算金额")
    from_currency: str = Field(description="源货币代码，如 CNY / USD / JPY")
    to_currency: str = Field(description="目标货币代码")

    @field_validator("from_currency", "to_currency")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) != 3:
            raise ValueError(f"货币代码必须是 3 位字母，收到 {v!r}")
        return v


def _fetch_rates(base: str) -> tuple[dict[str, float] | None, str]:
    """拉取以 base 为基准的汇率表。失败返回 (None, 原因)。"""
    url = env("EXCHANGE_RATE_API_URL")
    if not url:
        return None, "未配置汇率 API"
    try:
        timeout = tool_timeout()
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{url.rstrip('/')}/{base}")
            resp.raise_for_status()
            payload = resp.json()
        rates = payload.get("rates")
        if isinstance(rates, dict) and rates:
            return {k.upper(): float(v) for k, v in rates.items()}, "online"
    except Exception as exc:  # noqa: BLE001 - 任何网络问题都降级，不抛出
        return None, f"在线汇率不可用（{type(exc).__name__}）"
    return None, "在线汇率返回格式异常"


def _offline_rate(src: str, dst: str) -> float | None:
    """用 CNY 作为中转货币计算离线汇率。"""
    src_cny = FALLBACK_TO_CNY.get(src)
    dst_cny = FALLBACK_TO_CNY.get(dst)
    if src_cny is None or dst_cny is None:
        return None
    return src_cny / dst_cny


@register(
    name="convert_currency",
    description=(
        "货币换算。用于把外币预算折算成人民币（或反向）。"
        "网络不可用时自动使用内置离线汇率表，并在结果中标注来源。"
    ),
    params_model=CurrencyParams,
    idempotent=True,
    tags=["外部API", "换算"],
)
def convert_currency(params: CurrencyParams) -> ToolResult:
    src, dst = params.from_currency, params.to_currency

    if src == dst:
        return ToolResult.success(
            {
                "amount": params.amount,
                "from": src,
                "to": dst,
                "rate": 1.0,
                "result": round(params.amount, 2),
                "rate_source": "identity",
                "note": "源与目标货币相同，无需换算。",
            },
            source="identity",
        )

    rates, origin = _fetch_rates(src)
    rate: float | None = None
    rate_source = origin

    if rates and dst in rates:
        rate = rates[dst]
    else:
        rate = _offline_rate(src, dst)
        rate_source = "offline-fallback"

    if rate is None:
        return ToolResult.failure(
            ERR_BAD_ARGS,
            f"不支持的货币组合 {src}->{dst}，且在线汇率不可用。"
            f"离线表支持: {', '.join(sorted(FALLBACK_TO_CNY))}",
        )

    note = ""
    if rate_source == "offline-fallback":
        note = "⚠️ 在线汇率不可用，已使用内置离线汇率（近似值），请以实际成交价为准。"

    return ToolResult.success(
        {
            "amount": params.amount,
            "from": src,
            "to": dst,
            "rate": round(rate, 6),
            "result": round(params.amount * rate, 2),
            "rate_source": rate_source,
            "note": note,
        },
        source=rate_source,
    )
