"""工具执行层：两种实现，接口一致。

- ``LocalToolRunner``：进程内直调工具函数。用于单元测试与 MCP 不可用时的降级。
- ``MCPToolRunner``：通过 MCP 协议（stdio）调用独立进程中的工具。

两者都暴露 ``list_tool_schemas()`` 与 ``call()``，编排循环对此无感知。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Protocol

from common.envelope import ERR_UPSTREAM_ERROR, ToolResult
from mcp_server.tools.base import TOOL_REGISTRY, invoke, load_all_tools, openai_tool_schemas


class ToolRunner(Protocol):
    """编排循环依赖的最小接口。"""

    def list_tool_schemas(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult: ...

    async def aclose(self) -> None: ...


class LocalToolRunner:
    """进程内直调。测试友好、零 IPC 开销。"""

    def __init__(self) -> None:
        load_all_tools()

    def list_tool_schemas(self) -> list[dict[str, Any]]:
        return openai_tool_schemas()

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        # invoke() 是同步的：天气等外部 API 工具会阻塞 1–2 秒，重试时还会 sleep。
        # 放进线程池执行，否则会拖住整个事件循环（Web 服务下表现为所有请求一起卡住）。
        return await asyncio.to_thread(invoke, name, args)

    async def aclose(self) -> None:
        return None


class MCPToolRunner:
    """通过 MCP stdio 协议调用工具。

    用法::

        async with MCPToolRunner() as runner:
            await runner.call("get_weather_forecast", {...})
    """

    def __init__(self, command: str | None = None, args: list[str] | None = None) -> None:
        self._command = command or sys.executable
        self._args = args or ["-m", "mcp_server.server"]
        self._session = None
        self._stdio_cm = None
        self._session_cm = None

    async def __aenter__(self) -> "MCPToolRunner":
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        # 把项目根目录塞进 PYTHONPATH，保证子进程能 import 到 mcp_server
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [root, env.get("PYTHONPATH", "")]))

        params = StdioServerParameters(command=self._command, args=self._args, env=env)
        self._stdio_cm = stdio_client(params)
        read_stream, write_stream = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(*exc_info)
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(*exc_info)

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)

    def list_tool_schemas(self) -> list[dict[str, Any]]:
        """同步返回 Schema。

        MCP 的 list_tools 是异步的，但 Schema 在编译期就固定，
        这里直接用同一份注册表生成，避免为了拿 Schema 而多一次 RPC。
        真正的工具发现仍走 MCP（见 ``list_remote_tools``）。
        """
        load_all_tools()
        return openai_tool_schemas()

    async def list_remote_tools(self) -> list[str]:
        """通过 MCP 协议真实拉取远端工具名，用于验证桥接链路。"""
        assert self._session is not None, "请先进入 async with 上下文"
        resp = await self._session.list_tools()
        return [t.name for t in resp.tools]

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        assert self._session is not None, "请先进入 async with 上下文"
        try:
            resp = await self._session.call_tool(name, args or {})
        except Exception as exc:  # noqa: BLE001 - MCP 层异常统一兜底
            return ToolResult.failure(
                ERR_UPSTREAM_ERROR, f"MCP 调用失败: {type(exc).__name__}: {exc}", tool=name
            )

        text = _first_text(resp)

        # 工具失败时 MCP 会置 is_error，但我们自己的信封里带着更精确的错误码，
        # 因此优先解析信封原样透传，只有解析不出来才降级成 UPSTREAM_ERROR。
        if text is not None:
            try:
                return ToolResult.model_validate(json.loads(text))
            except Exception as exc:  # noqa: BLE001
                return ToolResult.failure(
                    ERR_UPSTREAM_ERROR, f"MCP 返回内容无法解析: {exc}", tool=name
                )

        if getattr(resp, "is_error", False) or getattr(resp, "isError", False):
            return ToolResult.failure(ERR_UPSTREAM_ERROR, "MCP 工具返回错误", tool=name)

        return ToolResult.failure(ERR_UPSTREAM_ERROR, "MCP 返回内容为空", tool=name)


def _first_text(resp: Any) -> str | None:
    for item in getattr(resp, "content", []) or []:
        if getattr(item, "type", None) == "text":
            return item.text
    return None


def registered_tool_names() -> list[str]:
    load_all_tools()
    return sorted(TOOL_REGISTRY)
