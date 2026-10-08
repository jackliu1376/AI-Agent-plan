"""MCP Server：把 ``TOOL_REGISTRY`` 中的工具以 MCP 协议（stdio）暴露出去。

为什么不用 ``MCPServer.tool()`` 装饰器：
装饰器会从**函数签名**推导 ``inputSchema``，导致「Pydantic 模型」与「函数签名」
两处定义漂移。这里改用低层 ``Server`` 的 ``on_list_tools`` / ``on_call_tool``
回调，直接拿注册表里的 Pydantic 模型生成 Schema，
保证 MCP 与 LLM Function Calling 用的是同一份定义（单一事实来源）。

适配的 SDK：``mcp >= 2.2``（2.x 中 FastMCP 已更名为 MCPServer，低层回调改为构造参数）。

启动方式::

    uv run python -m mcp_server.server          # 启动 stdio server
    uv run python -m mcp_server.server --list   # 只打印工具清单，便于调试
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from mcp_server.tools.base import load_all_tools, openai_tool_schemas

SERVER_NAME = "task-planner"
SERVER_VERSION = "0.1.0"


def build_server() -> Server:
    """构建并返回 MCP Server 实例。"""
    registry = load_all_tools()

    async def on_list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        """把注册表里的工具全部暴露出去。"""
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    input_schema=spec.openai_schema()["function"]["parameters"],
                )
                for spec in registry.values()
            ]
        )

    async def on_call_tool(
        ctx: Any, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """执行工具，并把统一信封序列化为文本内容返回。"""
        # 延迟导入，避免在模块导入阶段就拉起网络依赖
        from mcp_server.tools.base import invoke

        # invoke() 是**同步**的：天气 / 网页抓取会阻塞 1–2 秒，重试时还会 sleep。
        # 直接调用会把整个 MCP Server 的事件循环卡住 —— 无法并发处理调用，
        # 超时期间服务完全无响应。放进线程池，与 LocalToolRunner.call() 保持一致。
        result = await asyncio.to_thread(
            invoke, params.name, dict(params.arguments or {})
        )
        payload = json.dumps(result.to_payload(), ensure_ascii=False, default=str)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=payload)],
            is_error=not result.ok,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        # 工具数从注册表动态取 —— 写死数字必然随工具增删而过期
        # （这里曾经写着「共 8 个工具」，实际已是 10 个，而且这段文字会进 LLM 上下文）。
        instructions=(
            "任务规划助手工具集。提供天气查询、网页抓取、本地景点库查询、"
            "预算 CSV 解析、汇率换算、城际交通估算、计划落盘与用户澄清"
            f"共 {len(registry)} 个工具。"
        ),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def _serve() -> None:
    app = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    if "--list" in sys.argv:
        load_all_tools()
        schemas = openai_tool_schemas()
        print(json.dumps(schemas, ensure_ascii=False, indent=2))
        print(f"\n共 {len(schemas)} 个工具。", file=sys.stderr)
        return
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
