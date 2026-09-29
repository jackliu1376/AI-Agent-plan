"""LLM 客户端：DeepSeek（OpenAI 兼容接口）。

对外只暴露一个规范化后的 ``LLMResponse``，这样编排循环不关心底层是哪家模型，
测试里也能用 ``ScriptedLLM`` 直接替换。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.config import Settings


@dataclass
class ToolCallRequest:
    """模型发起的一次工具调用请求。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""


@dataclass
class LLMResponse:
    """规范化后的模型回复。"""

    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    # 可直接 append 回 messages 的 assistant 消息
    raw_message: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""


class LLMClient(Protocol):
    """编排循环依赖的最小 LLM 接口。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...


class DeepSeekClient:
    """基于 openai SDK 调用 DeepSeek。

    DeepSeek 的 ``/chat/completions`` 与 OpenAI 完全兼容，
    因此只需要替换 ``base_url`` 与 ``model``。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        from openai import OpenAI

        self.settings = settings or Settings.load()
        if not self.settings.has_credentials:
            raise RuntimeError(
                "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入真实 Key。"
            )
        # 提前拦截常见配置错误（例如误填 /anthropic 端点），
        # 否则要等到第一次请求才以 404 的形式暴露出来，很难定位。
        problem = self.settings.validate()
        if problem:
            raise RuntimeError(problem)

        self._client = OpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.normalized_base_url,
        )
        self._model = self.settings.model

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        msg = choice.message

        tool_calls: list[ToolCallRequest] = []
        raw_tool_calls: list[dict[str, Any]] = []
        for call in msg.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = {}
            tool_calls.append(
                ToolCallRequest(
                    id=call.id,
                    name=call.function.name,
                    arguments=parsed if isinstance(parsed, dict) else {},
                    raw_arguments=raw_args,
                )
            )
            raw_tool_calls.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.function.name, "arguments": raw_args},
                }
            )

        raw_message: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if raw_tool_calls:
            raw_message["tool_calls"] = raw_tool_calls

        usage = {}
        if completion.usage is not None:
            usage = {
                "prompt_tokens": completion.usage.prompt_tokens,
                "completion_tokens": completion.usage.completion_tokens,
                "total_tokens": completion.usage.total_tokens,
            }

        return LLMResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            raw_message=raw_message,
            usage=usage,
            finish_reason=choice.finish_reason or "",
        )
