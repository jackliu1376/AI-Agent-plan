"""Agent 运行时配置（全部来自环境变量，零硬编码）。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

WEEKDAY_CN = "一二三四五六日"

# 已知的错误 base_url 后缀 → 人话解释。
# 踩过的坑：DeepSeek 同时提供 OpenAI 兼容端点与 Anthropic 兼容端点，
# 文档站上两者挨得很近，很容易把 /anthropic 复制过来。
# 结果就是 SDK 拼出 https://api.deepseek.com/anthropic/chat/completions → 404。
KNOWN_BAD_BASE_URL_SUFFIXES: dict[str, str] = {
    "/anthropic": (
        "这是 DeepSeek 的 Anthropic 兼容端点（供 Claude Code 等工具使用），"
        "而本项目走的是 OpenAI 兼容协议"
    ),
    "/beta": "这是 DeepSeek 的 beta 端点，不是 OpenAI 兼容的正式端点",
    "/v1/chat/completions": "这里应该填 base_url（到 /v1 为止），而不是完整的请求路径",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _warn_fallback(name: str, raw: str, default: object, why: str) -> None:
    """配置非法时**明确告警**，而不是静默回落。

    静默回落比想象中危险：用户写了 ``MAX_TURNS=-1``，程序照常启动、
    用默认值 8 跑得好好的 —— 他以为自己配的 -1 生效了（或以为配置没被读到），
    直到某天发现行为不对才回头查。一行 stderr 就能省掉这次排查。

    写 stderr 而不是 stdout：stdout 在 CLI 里要留给计划正文。
    """
    print(
        f"⚠️  配置 {name}={raw!r} 非法（{why}），已回落到 {default!r}。",
        file=sys.stderr,
    )


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读整数环境变量。非法值（非数字 / 低于下限）回落到默认值**并告警**。

    早期版本只 catch ``ValueError``，导致 ``MAX_TURNS=-1`` 这类配置错误
    被静默接受 —— 编排循环会一轮都不跑，直接返回「未产出计划」，
    用户完全看不出是配置写错了。现在两件事都做：回落 + 告警。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _warn_fallback(name, raw, default, "不是整数")
        return default
    if minimum is not None and value < minimum:
        _warn_fallback(name, raw, default, f"低于下限 {minimum}")
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None,
               maximum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        _warn_fallback(name, raw, default, "不是数字")
        return default
    if minimum is not None and value < minimum:
        _warn_fallback(name, raw, default, f"低于下限 {minimum}")
        return default
    if maximum is not None and value > maximum:
        _warn_fallback(name, raw, default, f"超过上限 {maximum}")
        return default
    return value


def render_system_prompt(template: str, today: date | None = None) -> str:
    """把当前日期注入系统提示词。

    模型自身并不知道"今天"是哪一天，因此所有相对时间（"周末"、"下周三"、
    "下个月"）都必须由 Host 把当天日期显式喂给它；否则它只能瞎猜日期，
    或者反过来向用户提问本该自己推算的信息。
    """
    day = today or date.today()
    return template.replace("{{CURRENT_DATE}}", day.isoformat()).replace(
        "{{CURRENT_WEEKDAY}}", WEEKDAY_CN[day.weekday()]
    )


@dataclass(frozen=True)
class Settings:
    """一次性快照的运行参数。"""

    api_key: str
    base_url: str
    model: str
    max_turns: int
    tool_budget: int
    tool_timeout: int
    tool_retries: int
    prompt_path: Path

    # 可选调优项，带默认值（放最后，不破坏既有位置参数构造）
    llm_timeout: int = 60
    llm_max_retries: int = 2
    temperature: float = 0.2
    # 服务商私有参数的透传通道（JSON 字符串）。OpenAI 协议里没有这些，
    # 但不少服务商有额外开关，最典型的是关掉思考模式提速：
    #   LLM_EXTRA_BODY='{"thinking":{"type":"disabled"}}'
    # 做成透传而不是硬编码，是为了不把某一家的参数写进通用代码。
    extra_body: str = ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-xxxx")

    @property
    def normalized_base_url(self) -> str:
        """去掉结尾斜杠，避免拼出 `//chat/completions`。"""
        return self.base_url.rstrip("/")

    def validate(self) -> str | None:
        """返回配置错误的人话说明；None 表示配置正常。"""
        url = self.normalized_base_url
        if not url:
            return (
                "DEEPSEEK_BASE_URL 为空。\n"
                "请设置为：DEEPSEEK_BASE_URL=https://api.deepseek.com"
            )
        if not url.startswith(("http://", "https://")):
            return f"DEEPSEEK_BASE_URL 必须以 http:// 或 https:// 开头，当前为：{self.base_url}"

        for suffix, why in KNOWN_BAD_BASE_URL_SUFFIXES.items():
            if url.endswith(suffix):
                return (
                    f"DEEPSEEK_BASE_URL 配置有误：{self.base_url}\n"
                    f"原因：{why}。\n"
                    "SDK 会在此基础上拼接 /chat/completions，因此会返回 404。\n"
                    "请改为：DEEPSEEK_BASE_URL=https://api.deepseek.com"
                )
        return None

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            api_key=_env("DEEPSEEK_API_KEY"),
            base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            model=_env("DEEPSEEK_MODEL", "deepseek-chat"),
            max_turns=_env_int("MAX_TURNS", 8, minimum=1),
            tool_budget=_env_int("TOOL_BUDGET", 12, minimum=1),
            tool_timeout=_env_int("TOOL_TIMEOUT", 10, minimum=1),
            tool_retries=_env_int("TOOL_RETRIES", 1, minimum=0),
            prompt_path=PROJECT_ROOT / "agent" / "prompts" / "system.md",
            llm_timeout=_env_int("LLM_TIMEOUT", 60, minimum=5),
            llm_max_retries=_env_int("LLM_MAX_RETRIES", 2, minimum=0),
            temperature=_env_float("LLM_TEMPERATURE", 0.2, minimum=0.0, maximum=2.0),
            extra_body=_env("LLM_EXTRA_BODY"),
        )

    def system_prompt(self, today: date | None = None) -> str:
        """渲染系统提示词（注入当天日期）。"""
        try:
            template = self.prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"读取系统提示词失败：{self.prompt_path}\n原因：{exc}"
            ) from exc
        return render_system_prompt(template, today)
