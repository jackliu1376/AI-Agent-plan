"""工具注册表与统一调用框架。

设计要点
--------
1. **唯一事实来源**：每个工具在 ``TOOL_REGISTRY`` 中登记一次，
   MCP Server 与 LLM Function Calling 都从这里生成 Schema，避免两处漂移。
2. **统一信封**：调用方拿到的一定是 ``ToolResult``，永不抛裸异常。
3. **重试语义**：只对「瞬时故障」（超时 / 上游错误 / 未知异常）重试；
   参数错误、路径越界等确定性失败不重试。
4. **可观测**：每次调用记录 tool / latency_ms / attempts / source。
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from common.envelope import (
    ERR_BAD_ARGS,
    ERR_UNEXPECTED,
    ERR_UPSTREAM_ERROR,
    ERR_UPSTREAM_TIMEOUT,
    ToolResult,
)

try:  # httpx 是工具层的依赖；缺失时不影响核心层导入
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# 项目根目录（task-planner/）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读整数环境变量。非法值回落到默认值，而不是让进程崩掉。"""
    try:
        value = int(env(name, str(default)))
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


# ---------------------------------------------------------------------------
# 运行期配置注入
# ---------------------------------------------------------------------------
#
# 工具需要「超时」和「重试次数」两个参数。它们既可能来自环境变量，
# 也可能来自 ``Settings``（由编排循环注入）。
#
# 早期版本里工具直接读 env，导致 ``Settings.tool_timeout`` 成为死字段 ——
# 测试里构造 ``Settings(tool_timeout=1)`` 完全不生效。
# 现在统一走这两个函数：优先取注入值，否则回落环境变量。

_tool_timeout: ContextVar[int | None] = ContextVar("tool_timeout", default=None)
_tool_retries: ContextVar[int | None] = ContextVar("tool_retries", default=None)

DEFAULT_TOOL_TIMEOUT = 10
DEFAULT_TOOL_RETRIES = 1


def tool_timeout(default: int = DEFAULT_TOOL_TIMEOUT) -> int:
    """当前生效的工具超时（秒）。"""
    override = _tool_timeout.get()
    return override if override is not None else env_int("TOOL_TIMEOUT", default, minimum=1)


def tool_retries(default: int = DEFAULT_TOOL_RETRIES) -> int:
    """当前生效的工具重试次数。"""
    override = _tool_retries.get()
    return override if override is not None else env_int("TOOL_RETRIES", default, minimum=0)


@contextmanager
def tool_settings(*, timeout: int | None = None, retries: int | None = None) -> Iterator[None]:
    """在 ``with`` 块内覆盖工具超时 / 重试次数。

    配合 ``asyncio.to_thread`` 使用：线程会复制当前 context，因此覆盖值能带进去。
    """
    tokens: list[tuple[ContextVar[int | None], Any]] = []
    if timeout is not None:
        tokens.append((_tool_timeout, _tool_timeout.set(timeout)))
    if retries is not None:
        tokens.append((_tool_retries, _tool_retries.set(retries)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _is_timeout(exc: BaseException) -> bool:
    """判断是否为超时异常。

    注意 ``httpx.TimeoutException`` 继承自 ``httpx.HTTPError`` 而**不是**内置
    ``TimeoutError``，所以只写 ``except TimeoutError`` 是抓不到 httpx 超时的。
    """
    if httpx is not None and isinstance(exc, httpx.TimeoutException):
        return True
    return "timeout" in type(exc).__name__.lower()


# 瞬时故障 —— 值得重试
RETRYABLE = {ERR_UPSTREAM_TIMEOUT, ERR_UPSTREAM_ERROR, ERR_UNEXPECTED}


@dataclass
class ToolSpec:
    """一个工具的完整登记信息。"""

    name: str
    description: str
    params_model: type[BaseModel]
    func: Callable[..., ToolResult]
    idempotent: bool = True
    tags: list[str] = field(default_factory=list)

    def openai_schema(self) -> dict[str, Any]:
        """生成 OpenAI / DeepSeek Function Calling 格式的 Schema。"""
        schema = self.params_model.model_json_schema()
        # Pydantic 会塞入 title 等噪音字段，清理掉以节省 token
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register(
    name: str,
    description: str,
    params_model: type[BaseModel],
    *,
    idempotent: bool = True,
    tags: list[str] | None = None,
) -> Callable[[Callable[..., ToolResult]], Callable[..., ToolResult]]:
    """把函数登记进 ``TOOL_REGISTRY``。"""

    def decorator(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        if name in TOOL_REGISTRY:
            raise ValueError(f"工具名重复注册: {name}")
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            params_model=params_model,
            func=fn,
            idempotent=idempotent,
            tags=tags or [],
        )
        return fn

    return decorator


def openai_tool_schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """导出 Function Calling 的 ``tools`` 参数。"""
    specs = TOOL_REGISTRY.values() if names is None else (TOOL_REGISTRY[n] for n in names)
    return [spec.openai_schema() for spec in specs]


def invoke(name: str, args: dict[str, Any] | None = None, *, retries: int | None = None) -> ToolResult:
    """调用工具：校验参数 → 执行 → 计时 → 失败重试 → 统一错误映射。

    永远返回 ``ToolResult``，不会抛异常。
    """
    started = time.perf_counter()

    def _latency() -> int:
        return int((time.perf_counter() - started) * 1000)

    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return ToolResult.failure(
            ERR_BAD_ARGS, f"未知工具: {name}", tool=name, latency_ms=_latency()
        )

    args = args or {}
    max_retries = retries if retries is not None else tool_retries()
    attempt = 0

    while True:
        attempt += 1
        try:
            params = spec.params_model.model_validate(args)
        except ValidationError as exc:
            # 参数错误是确定性失败，不重试
            detail = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            return ToolResult.failure(
                ERR_BAD_ARGS,
                f"参数校验失败 -> {detail}",
                tool=name,
                latency_ms=_latency(),
                attempts=attempt,
            )

        try:
            result = spec.func(params)
            if not isinstance(result, ToolResult):  # 防御：工具实现忘了返回信封
                result = ToolResult.success(result)
            result.meta.tool = name
            result.meta.latency_ms = _latency()
            result.meta.attempts = attempt
            return result
        except Exception as exc:  # noqa: BLE001 - 统一兜底
            if _is_timeout(exc):
                code, message = ERR_UPSTREAM_TIMEOUT, f"上游超时: {exc}"
            else:
                code, message = ERR_UNEXPECTED, f"{type(exc).__name__}: {exc}"
            if env("TOOL_DEBUG") == "1":
                traceback.print_exc()

        if code in RETRYABLE and attempt <= max_retries:
            print(
                f"[tool:{name}] 第 {attempt} 次失败（{code}），准备重试…",
                file=sys.stderr,
            )
            time.sleep(min(0.4 * attempt, 1.5))  # 退避，避免打爆上游
            continue

        return ToolResult.failure(
            code, message, tool=name, latency_ms=_latency(), attempts=attempt
        )


def load_all_tools() -> dict[str, ToolSpec]:
    """导入所有工具模块，触发注册。返回注册表。"""
    from mcp_server.tools import (  # noqa: F401  仅为了触发副作用注册
        attractions,
        budget,
        clarify,
        currency,
        route,
        storage,
        weather,
        web,
    )

    return TOOL_REGISTRY
