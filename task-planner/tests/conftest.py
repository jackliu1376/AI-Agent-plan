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


@pytest.fixture(autouse=True)
def _isolate_run_log(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """测试默认不写运行记录，避免污染仓库里的 logs/runs.jsonl。

    想验证记录逻辑的测试，用 ``run_log_path`` fixture 显式指定路径
    （``record_run(path=...)`` 会绕过启用开关）。
    """
    monkeypatch.setenv("RUN_LOG_ENABLED", "0")
    monkeypatch.delenv("RUN_LABEL", raising=False)
    monkeypatch.setenv(
        "RUN_LOG_PATH", str(tmp_path_factory.mktemp("runlog") / "runs.jsonl")
    )


@pytest.fixture(autouse=True)
def _isolate_amap_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试默认**不带**高德 Key。

    否则同一份代码在不同开发者的机器上跑出不同结果 ——
    配了 Key 的走实时路线、没配的走离线估算，断言会随 ``.env`` 飘。
    需要 Key 的测试用 ``fake_amap``（自动设置）或显式 ``setenv``。
    """
    monkeypatch.delenv("AMAP_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_session_db(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """测试默认**不写**会话库，避免污染仓库里的 logs/sessions.db。

    默认关掉（而不是指到临时目录）是有意的：绝大多数测试关心的是
    内存里的会话状态机，不希望多一条隐性的磁盘依赖。
    要验证持久化的测试显式传 ``db=SessionDB(...)``，或用 ``session_db`` fixture。

    路径用 ``getbasetemp()`` 而不是 ``mktemp()`` —— 后者每条用例都会
    新建一个编号目录（pytest 只保留最近 3 个，其余要删），几百条用例
    会产生大量临时目录读写。这里的路径只是「万一有人打开了开关」的兜底，
    不需要独立目录。
    """
    monkeypatch.setenv("SESSION_DB_ENABLED", "0")
    monkeypatch.setenv(
        "SESSION_DB_PATH", str(tmp_path_factory.getbasetemp() / "sessions.db")
    )


@pytest.fixture
def session_db(tmp_path: Path):
    """一个指向临时目录的会话库，用完即弃。"""
    from web.session_db import SessionDB

    return SessionDB(tmp_path / "sessions.db")


@pytest.fixture
def run_log_path(tmp_path: Path) -> Path:
    """显式可用的运行记录路径（配合 ``record_run(path=...)``）。"""
    return tmp_path / "runs.jsonl"


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


# ---------------------------------------------------------------------------
# 假高德驾车 API
# ---------------------------------------------------------------------------

AMAP_OK_PAYLOAD: dict[str, Any] = {
    "status": "1",
    "info": "OK",
    "infocode": "10000",
    "count": "1",
    "route": {
        "paths": [
            {
                "distance": "1950000",  # 米 → 1950 km
                "duration": "72000",  # 秒 → 20 h
                "tolls": "850",
            }
        ]
    },
}


class FakeAmapClient:
    """替身 httpx.Client：只实现 amap_driving 用到的那几个方法。"""

    def __init__(self, payload: Any = None, exc: Exception | None = None) -> None:
        self._payload = payload if payload is not None else AMAP_OK_PAYLOAD
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeAmapClient":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, url: str, params: dict[str, Any] | None = None):  # noqa: ANN201
        self.calls.append({"url": url, "params": params or {}})
        if self._exc is not None:
            raise self._exc

        import httpx

        payload = self._payload

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> Any:
                if isinstance(payload, str):
                    raise ValueError("not json")
                return payload

        return _Resp()


@pytest.fixture
def fake_amap(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """走通「高德实时路线」路径：配置 key 并替换 amap_driving。"""
    import mcp_server.tools.route as route_mod

    monkeypatch.setenv("AMAP_API_KEY", "test-amap-key")
    counters = {"calls": 0}

    def _fake(
        origin_lonlat: tuple[float, float],
        dest_lonlat: tuple[float, float],
        *,
        timeout: int | None = None,
    ) -> tuple[dict[str, float] | None, str]:
        counters["calls"] += 1
        return {"distance_km": 1950.0, "duration_hours": 20.0, "tolls": 850.0}, ""

    monkeypatch.setattr(route_mod, "amap_driving", _fake)
    return counters


@pytest.fixture
def no_amap_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """确保未配置高德 key，用于验证降级行为。"""
    monkeypatch.delenv("AMAP_API_KEY", raising=False)


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
    data_freshness: str = "天气为实时预报；票价/开放时间为 2026-10 核对的演示数据",
    verification_channels: list[str] | None = None,
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
        # 默认计划引用了 get_weather_forecast，因此必须交代时效与核实渠道
        "data_freshness": data_freshness,
        "verification_channels": (
            verification_channels
            if verification_channels is not None
            else ["景区官方微信公众号 / 官方小程序"]
        ),
    }
