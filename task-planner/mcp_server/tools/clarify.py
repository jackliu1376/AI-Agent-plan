"""工具 T8：ask_user_clarification —— Human-in-the-loop 澄清。

MCP 工具本身无法直接读键盘，因此这里只负责「发出中断信号」：
返回 ``meta.interrupt = "user_input"``，由编排循环捕获后向用户提问，
再把答案回灌给模型。这样工具的职责保持纯净（只声明意图），
交互细节留给 Host。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from common.envelope import ToolResult
from mcp_server.tools.base import register


class ClarifyParams(BaseModel):
    question: str = Field(description="要问用户的问题（可一次包含多个小问，用换行分隔）")
    options: list[str] = Field(
        default_factory=list, description="可选项（可为空，表示开放式提问）"
    )
    reason: str = Field(default="", description="为什么需要这个信息（帮助用户理解）")

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question 不能为空")
        return v.strip()

    @field_validator("options", mode="before")
    @classmethod
    def _coerce(cls, v):  # noqa: ANN001
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


@register(
    name="ask_user_clarification",
    description=(
        "当缺失关键信息（目的地、出行时间、预算、人数等）会显著改变计划时，"
        "调用本工具向用户提问。一次最多合并 3 个问题。"
        "信息已经足够时不要调用本工具。"
    ),
    params_model=ClarifyParams,
    idempotent=False,
    tags=["交互", "HITL"],
)
def ask_user_clarification(params: ClarifyParams) -> ToolResult:
    return ToolResult.success(
        {
            "question": params.question,
            "options": params.options,
            "reason": params.reason,
            "awaiting": "user_input",
        },
        interrupt="user_input",
        source="host:stdin",
    )
