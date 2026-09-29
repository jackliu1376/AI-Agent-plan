"""计划（Plan）的 Pydantic 数据模型 —— 全项目唯一事实来源。

Agent 的最终输出必须能被这里的模型校验通过，否则编排循环会触发一次修复重试。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from common.envelope import ToolError, ToolMeta, ToolResult  # noqa: F401  (对外转出)

__all__ = [
    "ToolError",
    "ToolMeta",
    "ToolResult",
    "Step",
    "Phase",
    "Risk",
    "Plan",
    "extract_json_block",
    "parse_plan",
]

# ---------------------------------------------------------------------------
# 计划结构
# ---------------------------------------------------------------------------

StepId = str


class Step(BaseModel):
    """一个可执行的步骤。"""

    id: StepId = Field(description="步骤唯一标识，如 S1")
    action: str = Field(description="具体动作，1-2 小时内可完成或可明确执行")
    tool: str | None = Field(default=None, description="所需工具名，无工具则 null")
    tool_args: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    depends_on: list[StepId] = Field(default_factory=list, description="前置步骤 id 列表")
    eta: str = Field(default="", description="预估耗时，如 '2h'")
    deliverable: str = Field(default="", description="该步的产出物")
    done_when: str = Field(default="", description="验收标准")
    data_source: str | None = Field(default=None, description="事实数据来源（工具名+关键参数）")

    @field_validator("depends_on", mode="before")
    @classmethod
    def _coerce_depends(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)


class Phase(BaseModel):
    """一个阶段，包含若干步骤。"""

    name: str
    steps: list[Step] = Field(default_factory=list)


class Risk(BaseModel):
    risk: str
    mitigation: str


class Plan(BaseModel):
    """任务规划助手的最终产出。"""

    goal: str = Field(description="对用户目标的复述")
    assumptions: list[str] = Field(default_factory=list, description="Agent 自行补充的假设")
    clarifications_needed: list[str] = Field(default_factory=list, description="仍需用户澄清的问题")
    phases: list[Phase] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    total_eta: str = ""
    budget_estimate: str = ""

    # -- 派生校验 ---------------------------------------------------------

    def all_steps(self) -> list[Step]:
        return [s for p in self.phases for s in p.steps]

    def check_dag(self) -> tuple[bool, str]:
        """校验 depends_on 是否构成无环图，且引用都存在。"""
        steps = {s.id: s for s in self.all_steps()}
        for step in steps.values():
            for dep in step.depends_on:
                if dep not in steps:
                    return False, f"步骤 {step.id} 依赖了不存在的 {dep}"

        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(steps, WHITE)

        def visit(node: str, path: list[str]) -> tuple[bool, str]:
            if color[node] == GRAY:
                return False, "存在循环依赖: " + " -> ".join([*path, node])
            if color[node] == BLACK:
                return True, ""
            color[node] = GRAY
            for dep in steps[node].depends_on:
                ok, msg = visit(dep, [*path, node])
                if not ok:
                    return False, msg
            color[node] = BLACK
            return True, ""

        for sid in steps:
            ok, msg = visit(sid, [])
            if not ok:
                return False, msg
        return True, "ok"

    def referenced_tools(self) -> set[str]:
        return {s.tool for s in self.all_steps() if s.tool}


# ---------------------------------------------------------------------------
# 从 LLM 自由文本中抽取 JSON
# ---------------------------------------------------------------------------

_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_block(text: str) -> dict[str, Any] | None:
    """从模型回复里抽出计划 JSON。

    优先取 ```json 围栏；没有围栏则退化为「首个 { 到最后一个 }」的粗匹配。
    """
    if not text:
        return None

    candidates: list[str] = [m.group(1) for m in _FENCED.finditer(text)]

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_plan(text: str) -> tuple[Plan | None, str]:
    """把模型回复解析成 Plan。返回 (plan, 错误信息)。"""
    obj = extract_json_block(text)
    if obj is None:
        return None, "回复中未找到可解析的 JSON 计划块"
    try:
        plan = Plan.model_validate(obj)
    except Exception as exc:  # noqa: BLE001 - 需要把校验细节回传给模型修复
        return None, f"计划结构校验失败: {exc}"
    return plan, ""
