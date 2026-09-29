"""工具包：导入即完成注册（副作用）。

``base.load_all_tools()`` 依赖这里的导入顺序来填充 ``TOOL_REGISTRY``。
"""

from mcp_server.tools.base import (  # noqa: F401
    TOOL_REGISTRY,
    ToolSpec,
    invoke,
    load_all_tools,
    openai_tool_schemas,
    register,
)
