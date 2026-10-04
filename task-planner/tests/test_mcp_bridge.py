"""MCP 桥接集成测试 —— 验证 BYOA 要求的「Context Integration」。

这些用例会真实拉起 ``mcp_server.server`` 子进程，走 stdio / JSON-RPC 完成
工具发现与调用，证明 Agent 的大脑确实是通过标准化协议连到本地环境的。
"""

from __future__ import annotations

import pytest

from agent.tool_runner import MCPToolRunner

EXPECTED = {
    "get_weather_forecast",
    "fetch_webpage",
    "query_attractions_db",
    "parse_budget_csv",
    "convert_currency",
    "estimate_route",
    "query_transit_options",
    "query_attraction_realtime",
    "save_itinerary",
    "ask_user_clarification",
}


async def test_mcp_server_exposes_all_tools() -> None:
    """通过 MCP 协议 list_tools，应拿到全部工具。"""
    async with MCPToolRunner() as runner:
        names = await runner.list_remote_tools()
    assert set(names) == EXPECTED


async def test_mcp_call_roundtrip() -> None:
    """通过 MCP 协议真实调用一个离线工具，验证信封能完整往返。"""
    async with MCPToolRunner() as runner:
        result = await runner.call(
            "estimate_route",
            {"origin": "上海", "destination": "成都", "mode": "hsr"},
        )
    assert result.ok, result.error
    assert result.data["origin"] == "上海"
    assert result.data["destination"] == "成都"
    assert result.data["distance_km"] > 0


async def test_mcp_reports_tool_error_without_crashing() -> None:
    """工具返回失败时，MCP 层应把错误信封透传回来，而不是抛异常。"""
    async with MCPToolRunner() as runner:
        result = await runner.call("query_attractions_db", {"city": "火星"})
    assert result.ok is False
    assert result.error.code == "NOT_FOUND"


async def test_mcp_schemas_match_local_registry() -> None:
    """MCP 暴露的 Schema 必须与本地注册表一致（单一事实来源）。"""
    from agent.tool_runner import LocalToolRunner

    async with MCPToolRunner() as runner:
        mcp_schemas = {s["function"]["name"]: s for s in runner.list_tool_schemas()}
    local_schemas = {
        s["function"]["name"]: s for s in LocalToolRunner().list_tool_schemas()
    }
    assert mcp_schemas == local_schemas
