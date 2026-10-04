"""计划（Plan）的 Pydantic 数据模型 —— 全项目唯一事实来源。

Agent 的最终输出必须能被这里的模型校验通过，否则编排循环会触发一次修复重试。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

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


# 会产出「会过期」的数据的工具。
# 计划里引用了其中任何一个，就必须交代数据时效与核实渠道（见 Plan._validate_freshness）。
FRESHNESS_TOOLS: frozenset[str] = frozenset(
    {
        "query_attractions_db",  # 票价 / 开放时间 / 评分
        "get_weather_forecast",  # 天气预报
    }
)


class Plan(BaseModel):
    """任务规划助手的最终产出。"""

    goal: str = Field(description="对用户目标的复述")
    assumptions: list[str] = Field(default_factory=list, description="Agent 自行补充的假设")
    clarifications_needed: list[str] = Field(default_factory=list, description="仍需用户澄清的问题")
    phases: list[Phase] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    total_eta: str = ""
    budget_estimate: str = ""

    # -- 数据时效与核实渠道 -----------------------------------------------
    #
    # 这两项是本项目对「数据可信度」的核心交代：种子库里的票价 / 开放时间 /
    # 评分是演示数据，计划里不说明的话，用户会误以为它们是实时的。
    #
    # 做成 **schema 字段 + 条件校验**，而不是只靠提示词约束 ——
    # 提示词可能被忽略，schema 校验不会。
    #
    # 「条件」很关键：只有引用了时效性数据源（景点库 / 天气）时才强制要求。
    # 无条件强制会让「帮我把毕业论文写完」这类非事实性任务也被迫编一句
    # 时效声明，反而制造噪音。

    data_freshness: str = Field(
        default="",
        description=(
            "数据时效声明：说明计划中事实性数据（票价、开放时间、评分、天气）"
            "的核对时间与可能变动，需写明核对月份。"
        ),
    )
    verification_channels: list[str] = Field(
        default_factory=list,
        description=(
            "建议用户自行核实信息的官方渠道，至少 1 条。"
            "例如「景区官方微信公众号 / 官方小程序」「景点官网」。"
            "不要编造具体网址，除非工具返回中确实提供了。"
        ),
    )

    # -- 结构校验 ---------------------------------------------------------
    #
    # 这几条不是「格式好看」，而是「计划是否可用」的底线。
    # 没有它们时，模型返回 {"goal":"x","phases":[]} 会被判定为成功，
    # 用户拿到空计划却被告知成功，批量评测也会把这类样本误计为通过。

    @model_validator(mode="after")
    def _validate_structure(self) -> "Plan":
        steps = self.all_steps()

        if not steps:
            raise ValueError(
                "计划至少需要 1 个步骤：phases 不能为空，且每个 phase 的 steps 不能为空"
            )

        ids = [s.id for s in steps]
        seen: set[str] = set()
        dupes: set[str] = set()
        for step_id in ids:
            if step_id in seen:
                dupes.add(step_id)
            seen.add(step_id)
        if dupes:
            raise ValueError(
                f"步骤 id 必须全局唯一，发现重复：{sorted(dupes)}。"
                "重复 id 会导致依赖关系指向错误的步骤。"
            )

        blank = [s.id for s in steps if not s.action.strip()]
        if blank:
            raise ValueError(f"以下步骤的 action 为空：{blank}")

        return self

    @model_validator(mode="after")
    def _validate_freshness(self) -> "Plan":
        """引用了时效性数据源时，必须交代数据时效与核实渠道。

        触发条件是「计划里真的用到了会过期的数据」，而不是「任务是不是旅游」——
        这样非事实性任务（写论文、做预算表）不会被强加一条无意义的声明。
        """
        used = self.referenced_tools()
        needs = used & FRESHNESS_TOOLS
        if not needs:
            return self

        sources = "、".join(sorted(needs))
        if not self.data_freshness.strip():
            raise ValueError(
                f"计划引用了时效性数据源（{sources}），因此 data_freshness 不能为空。"
                "请说明这些数据的核对时间（如「票价为 2026-10 核对的演示数据」）"
                "以及可能存在的变动。"
            )
        if not self.verification_channels:
            raise ValueError(
                f"计划引用了时效性数据源（{sources}），因此 verification_channels 不能为空。"
                "请至少给出 1 条用户可自行核实的官方渠道"
                "（如「景区官方微信公众号 / 小程序」「景点官网」）。"
            )
        return self

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
                if dep == step.id:
                    return False, f"步骤 {step.id} 依赖了自己"

        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(steps, WHITE)

        # 迭代式 DFS：超长依赖链（>1000 步）也不会触发递归深度限制
        for root in steps:
            if color[root] != WHITE:
                continue
            path: list[str] = []
            stack: list[tuple[str, int]] = [(root, 0)]
            while stack:
                node, index = stack.pop()
                if index == 0:
                    if color[node] == GRAY:
                        return False, "存在循环依赖: " + " -> ".join([*path, node])
                    if color[node] == BLACK:
                        continue
                    color[node] = GRAY
                    path.append(node)

                deps = steps[node].depends_on
                if index < len(deps):
                    stack.append((node, index + 1))
                    stack.append((deps[index], 0))
                else:
                    color[node] = BLACK
                    if path and path[-1] == node:
                        path.pop()
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
