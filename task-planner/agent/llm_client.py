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
            # SDK 默认超时 600 秒 —— 上游挂起时整个会话会静默卡 10 分钟。
            # Web 层表现为「该会话一直不动且没有任何事件」。
            timeout=float(self.settings.llm_timeout),
            max_retries=self.settings.llm_max_retries,
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
            "temperature": self.settings.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # 透传服务商私有参数（见 Settings.extra_body 的说明）。
        # 解析失败就明确报错 —— 静默忽略的话，用户以为关掉了思考，
        # 实际还在跑，只会觉得「怎么这么慢」而查不出原因。
        if self.settings.extra_body:
            try:
                parsed = json.loads(self.settings.extra_body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"LLM_EXTRA_BODY 不是合法 JSON：{exc}。"
                    '正确写法例：LLM_EXTRA_BODY=\'{"thinking":{"type":"disabled"}}\''
                ) from exc
            if isinstance(parsed, dict):
                kwargs["extra_body"] = parsed

        completion = self._client.chat.completions.create(**kwargs)

        if not completion.choices:
            raise RuntimeError(
                "模型返回了空的 choices（可能是内容被安全策略拦截）。"
                "请调整任务描述后重试。"
            )

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

        # 推理模型（MiMo 默认开启、GLM 传 thinking 时）会在返回 tool_calls 的
        # **同时**返回 reasoning_content。MiMo 官方文档建议多轮工具调用时把它
        # 一并回传 —— 丢掉等于让模型每一轮都从头「重新想」一遍。
        #
        # 只在字段确实存在时带上：DeepSeek 不返回这个字段，所以对它零影响。
        reasoning = getattr(msg, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            raw_message["reasoning_content"] = reasoning

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

    def close(self) -> None:
        """释放底层 HTTP 连接池。

        OpenAI SDK 内部持有 ``httpx.Client``。不显式关闭的话，连接会一直挂到
        GC 回收为止 —— Web 层每次运行都新建一个 client，会话一多就是
        fd / socket 持续累积。**绝不抛异常**：收尾失败不该影响调用方。
        """
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 见上
            pass
