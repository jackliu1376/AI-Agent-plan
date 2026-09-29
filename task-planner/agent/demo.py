"""离线演示用的脚本化 LLM。

用途：在没有 DeepSeek API Key 的情况下，也能完整演示
「模型决策 → 工具调用 → 结果回灌 → 产出结构化计划」这条链路，
方便课堂演示与自动化测试。
"""

from __future__ import annotations

import json
from typing import Any

from agent.llm_client import LLMResponse, ToolCallRequest

DEMO_PLAN: dict[str, Any] = {
    "goal": "成都 2 日亲子游（人均预算 ¥1000）",
    "assumptions": [
        "出发地默认上海，往返交通按高铁二等座估算",
        "出行日期取未来最近的周六、周日",
        "住宿按 1 晚经济型亲子酒店估算",
    ],
    "clarifications_needed": [],
    "phases": [
        {
            "name": "行前准备",
            "steps": [
                {
                    "id": "S1",
                    "action": "查询成都目标日期天气，据此决定户外/室内行程配比",
                    "tool": "get_weather_forecast",
                    "tool_args": {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"},
                    "depends_on": [],
                    "eta": "10min",
                    "deliverable": "两日天气与穿衣建议",
                    "done_when": "拿到每日最高/最低温与降水概率",
                    "data_source": "get_weather_forecast(city=成都, 2026-10-03..2026-10-04)",
                },
                {
                    "id": "S2",
                    "action": "查询成都亲子类、人均 200 元以内的景点候选清单",
                    "tool": "query_attractions_db",
                    "tool_args": {"city": "成都", "tags": ["亲子"], "max_price": 200, "limit": 10},
                    "depends_on": [],
                    "eta": "5min",
                    "deliverable": "亲子景点候选表（含人均票价与游玩时长）",
                    "done_when": "候选景点不少于 5 个",
                    "data_source": "query_attractions_db(city=成都, tags=[亲子], max_price=200)",
                },
                {
                    "id": "S3",
                    "action": "估算上海→成都往返高铁耗时与费用",
                    "tool": "estimate_route",
                    "tool_args": {"origin": "上海", "destination": "成都", "mode": "hsr"},
                    "depends_on": [],
                    "eta": "5min",
                    "deliverable": "单程耗时与票价量级",
                    "done_when": "得到往返交通费用估算值",
                    "data_source": "estimate_route(上海→成都, hsr)",
                },
                {
                    "id": "S4",
                    "action": "预订往返高铁票与 1 晚亲子酒店",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S1", "S3"],
                    "eta": "1h",
                    "deliverable": "车票与酒店订单",
                    "done_when": "订单确认短信到手",
                    "data_source": None,
                },
            ],
        },
        {
            "name": "行程执行",
            "steps": [
                {
                    "id": "S5",
                    "action": "Day1 上午：成都大熊猫繁育研究基地（建议 07:30 前入园）",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S4"],
                    "eta": "4h",
                    "deliverable": "熊猫基地游览完成",
                    "done_when": "完成核心园区游览",
                    "data_source": "query_attractions_db(city=成都)",
                },
                {
                    "id": "S6",
                    "action": "Day1 下午：武侯祠 + 锦里古街（步行可达，一并游览）",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S5"],
                    "eta": "4h",
                    "deliverable": "完成人文与美食体验",
                    "done_when": "游览完武侯祠与锦里",
                    "data_source": "query_attractions_db(city=成都)",
                },
                {
                    "id": "S7",
                    "action": "Day2 全天：都江堰 + 青城山（视天气决定是否成行）",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S1", "S4"],
                    "eta": "8h",
                    "deliverable": "世界遗产一日游",
                    "done_when": "完成都江堰与青城山游览",
                    "data_source": "get_weather_forecast + query_attractions_db",
                },
                {
                    "id": "S8",
                    "action": "若 Day2 降水概率≥50%，改用四川科技馆 + 成都博物馆室内方案",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S1"],
                    "eta": "0",
                    "deliverable": "雨天备选方案",
                    "done_when": "已确定备选路线",
                    "data_source": "get_weather_forecast(city=成都)",
                },
            ],
        },
        {
            "name": "收尾",
            "steps": [
                {
                    "id": "S9",
                    "action": "Day2 晚返程，整理票据并复盘实际花费",
                    "tool": None,
                    "tool_args": {},
                    "depends_on": ["S7", "S8"],
                    "eta": "1h",
                    "deliverable": "费用复盘表",
                    "done_when": "实际花费与预算差额记录在案",
                    "data_source": None,
                },
                {
                    "id": "S10",
                    "action": "把最终行程落盘保存",
                    "tool": "save_itinerary",
                    "tool_args": {"path": "chengdu-2d.md", "content": "# 成都 2 日亲子游"},
                    "depends_on": ["S9"],
                    "eta": "5min",
                    "deliverable": "outputs/chengdu-2d.md",
                    "done_when": "文件已生成",
                    "data_source": None,
                },
            ],
        },
    ],
    "risks": [
        {"risk": "熊猫基地旺季限流", "mitigation": "提前在官方渠道实名预约并 07:30 前入园"},
        {"risk": "Day2 降雨影响青城山", "mitigation": "启用 S8 室内备选方案"},
        {"risk": "高铁票紧张", "mitigation": "放票第一时间抢票，备选飞机"},
    ],
    "total_eta": "约 2 天 + 行前准备 1.5h",
    "budget_estimate": "¥980/人",
}

DEMO_MARKDOWN = """# 成都 2 日亲子游计划

> 已调用 3 个工具取证：天气预报、景点库查询、交通估算。

## 阶段一 · 行前准备
| 步骤 | 动作 | 工具 | 依赖 | 产出 |
|---|---|---|---|---|
| S1 | 查成都目标日期天气 | get_weather_forecast | - | 天气与穿衣建议 |
| S2 | 查亲子景点候选 | query_attractions_db | - | 景点候选表 |
| S3 | 估算往返高铁 | estimate_route | - | 交通费用量级 |
| S4 | 订票订酒店 | - | S1,S3 | 订单确认 |

## 阶段二 · 行程执行
| 步骤 | 动作 | 依赖 |
|---|---|---|
| S5 | Day1 上午 熊猫基地 | S4 |
| S6 | Day1 下午 武侯祠 + 锦里 | S5 |
| S7 | Day2 都江堰 + 青城山 | S1,S4 |
| S8 | 雨天备选：科技馆 + 博物馆 | S1 |

## 阶段三 · 收尾
| 步骤 | 动作 | 依赖 |
|---|---|---|
| S9 | 返程与费用复盘 | S7,S8 |
| S10 | 行程落盘 | S9 |
"""


def build_demo_script() -> list[LLMResponse]:
    """构造一段两轮的演示脚本：先取证，再出计划。"""
    turn1 = LLMResponse(
        content="先取证：查询成都天气、亲子景点候选与往返交通。",
        tool_calls=[
            ToolCallRequest(
                id="call_w1",
                name="get_weather_forecast",
                arguments={"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"},
                raw_arguments='{"city":"成都"}',
            ),
            ToolCallRequest(
                id="call_a1",
                name="query_attractions_db",
                arguments={"city": "成都", "tags": ["亲子"], "max_price": 200, "limit": 10},
                raw_arguments='{"city":"成都"}',
            ),
            ToolCallRequest(
                id="call_r1",
                name="estimate_route",
                arguments={"origin": "上海", "destination": "成都", "mode": "hsr"},
                raw_arguments='{"origin":"上海"}',
            ),
        ],
        raw_message={
            "role": "assistant",
            "content": "先取证：查询成都天气、亲子景点候选与往返交通。",
            "tool_calls": [
                {"id": "call_w1", "type": "function",
                 "function": {"name": "get_weather_forecast", "arguments": '{"city":"成都"}'}},
                {"id": "call_a1", "type": "function",
                 "function": {"name": "query_attractions_db", "arguments": '{"city":"成都"}'}},
                {"id": "call_r1", "type": "function",
                 "function": {"name": "estimate_route", "arguments": '{"origin":"上海"}'}},
            ],
        },
        usage={"prompt_tokens": 820, "completion_tokens": 96, "total_tokens": 916},
        finish_reason="tool_calls",
    )

    turn2 = LLMResponse(
        content=DEMO_MARKDOWN + "\n```json\n" + json.dumps(DEMO_PLAN, ensure_ascii=False, indent=2) + "\n```",
        tool_calls=[],
        raw_message={"role": "assistant", "content": DEMO_MARKDOWN},
        usage={"prompt_tokens": 1540, "completion_tokens": 1180, "total_tokens": 2720},
        finish_reason="stop",
    )
    return [turn1, turn2]


class ScriptedLLM:
    """按剧本顺序返回预设回复，剧本用完后重复最后一条。"""

    def __init__(self, script: list[LLMResponse]) -> None:
        if not script:
            raise ValueError("剧本不能为空")
        self._script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None) -> LLMResponse:  # noqa: ANN001
        response = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return response
