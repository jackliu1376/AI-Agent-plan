"""pytest 公共夹具：假天气 API、脚本化 LLM、隔离的落盘沙箱。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.config import PROJECT_ROOT, Settings
from agent.llm_client import LLMResponse, ToolCallRequest
from agent.tool_runner import LocalToolRunner

# ---------------------------------------------------------------------------
# 基础夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """测试用配置：不依赖真实 API Key。"""
    return Settings(
        api_key="",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        max_turns=8,
        tool_budget=12,
        tool_timeout=5,
        tool_retries=1,
        prompt_path=PROJECT_ROOT / "agent" / "prompts" / "system.md",
    )


@pytest.fixture
def runner() -> LocalToolRunner:
    return LocalToolRunner()


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """把落盘目录指向临时目录，避免污染项目 outputs/。"""
    target = tmp_path / "outputs"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OUTPUT_DIR", str(target))
    return target


# ---------------------------------------------------------------------------
# 假天气 API
# ---------------------------------------------------------------------------

GEOCODE_PAYLOAD: dict[str, Any] = {
    "results": [{"latitude": 30.5728, "longitude": 104.0668, "name": "成都"}]
}

FORECAST_PAYLOAD: dict[str, Any] = {
    "daily": {
        "time": ["2026-10-03", "2026-10-04"],
        "temperature_2m_max": [24.5, 21.0],
        "temperature_2m_min": [16.2, 15.1],
        "precipitation_probability_max": [20, 70],
        "weathercode": [2, 61],
    }
}


@pytest.fixture
def fake_weather(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """把 Open-Meteo 换成固定响应，记录调用次数。"""
    import mcp_server.tools.weather as weather_mod

    counters = {"geocode": 0, "forecast": 0}

    def _fake_fetch(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if "geocoding" in url:
            counters["geocode"] += 1
            return GEOCODE_PAYLOAD
        counters["forecast"] += 1
        return FORECAST_PAYLOAD

    monkeypatch.setattr(weather_mod, "_fetch_json", _fake_fetch)
    return counters


@pytest.fixture
def broken_weather(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """让天气工具持续失败，用于验证重试 + 降级。"""
    import mcp_server.tools.weather as weather_mod

    counters = {"attempts": 0}

    def _boom(url: str, params: dict[str, Any]) -> dict[str, Any]:
        counters["attempts"] += 1
        raise TimeoutError("模拟上游超时")

    monkeypatch.setattr(weather_mod, "_fetch_json", _boom)
    return counters


# ---------------------------------------------------------------------------
# 脚本化 LLM 构造器
# ---------------------------------------------------------------------------


def make_tool_call(
    call_id: str, name: str, arguments: dict[str, Any] | None = None
) -> ToolCallRequest:
    args = arguments or {}
    return ToolCallRequest(
        id=call_id,
        name=name,
        arguments=args,
        raw_arguments=json.dumps(args, ensure_ascii=False),
    )


def tool_turn(*calls: tuple[str, str, dict[str, Any]]) -> LLMResponse:
    """构造一轮「模型请求调用工具」的回复。入参为 (call_id, tool_name, args)。"""
    requests = [make_tool_call(cid, name, args) for cid, name, args in calls]
    raw_calls = [
        {
            "id": req.id,
            "type": "function",
            "function": {"name": req.name, "arguments": req.raw_arguments},
        }
        for req in requests
    ]
    return LLMResponse(
        content="正在取证……",
        tool_calls=requests,
        raw_message={"role": "assistant", "content": "正在取证……", "tool_calls": raw_calls},
        usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        finish_reason="tool_calls",
    )


def text_turn(text: str) -> LLMResponse:
    """构造一轮「模型给出最终回复」的回复。"""
    return LLMResponse(
        content=text,
        tool_calls=[],
        raw_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 200, "completion_tokens": 300, "total_tokens": 500},
        finish_reason="stop",
    )


def plan_turn(plan: dict[str, Any], markdown: str = "## 计划\n- 步骤一\n") -> LLMResponse:
    """构造一轮「模型输出 Markdown + JSON 计划」的回复。"""
    body = markdown + "\n```json\n" + json.dumps(plan, ensure_ascii=False, indent=2) + "\n```"
    return text_turn(body)


# ---------------------------------------------------------------------------
# 常用计划样例
# ---------------------------------------------------------------------------


def make_plan(
    *,
    goal: str = "测试计划",
    phases: list[dict[str, Any]] | None = None,
    assumptions: list[str] | None = None,
    risks: list[dict[str, str]] | None = None,
    total_eta: str = "2 天",
    budget_estimate: str = "¥1000",
) -> dict[str, Any]:
    return {
        "goal": goal,
        "assumptions": assumptions or [],
        "clarifications_needed": [],
        "phases": phases
        if phases is not None
        else [
            {
                "name": "准备",
                "steps": [
                    {
                        "id": "S1",
                        "action": "查天气",
                        "tool": "get_weather_forecast",
                        "tool_args": {
                            "city": "成都",
                            "start_date": "2026-10-03",
                            "end_date": "2026-10-04",
                        },
                        "depends_on": [],
                        "eta": "10min",
                        "deliverable": "天气表",
                        "done_when": "拿到数据",
                        "data_source": "get_weather_forecast(成都)",
                    }
                ],
            },
            {
                "name": "执行",
                "steps": [
                    {
                        "id": "S2",
                        "action": "按天气安排行程",
                        "tool": None,
                        "tool_args": {},
                        "depends_on": ["S1"],
                        "eta": "1d",
                        "deliverable": "行程",
                        "done_when": "走完",
                        "data_source": None,
                    }
                ],
            },
        ],
        "risks": risks or [{"risk": "天气变化", "mitigation": "备选室内方案"}],
        "total_eta": total_eta,
        "budget_estimate": budget_estimate,
    }
