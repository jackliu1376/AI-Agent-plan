"""工具返回信封 —— 所有工具的统一数据契约。

约定：任何工具都不允许返回裸字符串或抛裸异常，
必须返回 ``ToolResult``，形如 ``{ok, data, error, meta}``。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 错误码常量（集中管理，便于测试断言）
ERR_BAD_ARGS = "BAD_ARGS"
ERR_UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
ERR_UPSTREAM_ERROR = "UPSTREAM_ERROR"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_FORBIDDEN_PATH = "FORBIDDEN_PATH"
ERR_FILE_ERROR = "FILE_ERROR"
ERR_UNEXPECTED = "UNEXPECTED"
# 参数本身合法，但**取值超出了数据源能覆盖的范围** —— 典型是「查 40 天后的
# 天气」：日期格式没问题，只是预报只有约 16 天。
# 和 BAD_ARGS 分开是有必要的：BAD_ARGS 的潜台词是「你传错了」，
# 模型看到会去改格式、重试同一个日期；而这个码的潜台词是
# 「这个日期问不出结果，换成区间内的日期，或者改用气候经验值」。
ERR_OUT_OF_RANGE = "OUT_OF_RANGE"
# 编排循环专用：工具预算耗尽（不是工具本身的问题，但同样要记进 trace）
ERR_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class ToolError(BaseModel):
    """结构化错误，禁止把裸异常字符串抛给模型。"""

    code: str = Field(description="机器可读错误码，如 UPSTREAM_TIMEOUT")
    message: str = Field(description="人类可读的说明")


class ToolMeta(BaseModel):
    """工具调用的可观测元信息。"""

    tool: str = ""
    latency_ms: int = 0
    cached: bool = False
    source: str = ""
    attempts: int = 1
    # 值为 "user_input" 时，编排循环会暂停并向用户提问（ask_user_clarification 用）
    interrupt: Literal["user_input"] | None = None


class ToolResult(BaseModel):
    """统一返回信封。"""

    ok: bool
    data: Any = None
    error: ToolError | None = None
    meta: ToolMeta = Field(default_factory=ToolMeta)

    # -- 构造快捷方式 -----------------------------------------------------

    @classmethod
    def success(cls, data: Any, **meta: Any) -> "ToolResult":
        return cls(ok=True, data=data, meta=ToolMeta(**meta))

    @classmethod
    def failure(cls, code: str, message: str, **meta: Any) -> "ToolResult":
        return cls(ok=False, error=ToolError(code=code, message=message), meta=ToolMeta(**meta))

    # -- 序列化 -----------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """转成喂给 LLM 的 JSON（去掉 None 噪音，保持 token 经济）。"""
        return self.model_dump(exclude_none=True)

    def needs_user_input(self) -> bool:
        return self.meta.interrupt == "user_input"
