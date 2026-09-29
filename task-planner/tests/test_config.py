"""配置层测试：base_url 校验 与 系统提示词的日期注入。

这两个点都是「实跑时踩到才发现的坑」，因此必须钉上回归测试：

1. `DEEPSEEK_BASE_URL` 误填 Anthropic 端点 → SDK 拼出
   `https://api.deepseek.com/anthropic/chat/completions` → 404。
   现在必须在构造客户端时就报出人话错误，而不是等到第一次请求。
2. 模型不知道"今天"是哪天，导致它无法把"周末"/"下周三"换算成具体日期，
   反而向用户提问本该自己推算的信息。
"""

from __future__ import annotations

import dataclasses
import os
import re
from datetime import date
from pathlib import Path

import pytest

from agent.config import PROJECT_ROOT, Settings, render_system_prompt

PROMPT_PATH = PROJECT_ROOT / "agent" / "prompts" / "system.md"


def make_settings(**overrides) -> Settings:
    base = Settings(
        api_key="sk-test-key-0123456789",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        max_turns=8,
        tool_budget=12,
        tool_timeout=10,
        tool_retries=1,
        prompt_path=PROMPT_PATH,
    )
    return dataclasses.replace(base, **overrides)


# ---------------------------------------------------------------------------
# base_url 校验
# ---------------------------------------------------------------------------


def test_validate_accepts_official_endpoints() -> None:
    for url in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        assert make_settings(base_url=url).validate() is None, url


def test_validate_rejects_anthropic_endpoint() -> None:
    """本次实跑踩到的真实坑。"""
    problem = make_settings(base_url="https://api.deepseek.com/anthropic").validate()
    assert problem is not None
    assert "/anthropic" in problem
    assert "Anthropic 兼容端点" in problem
    # 错误信息必须直接给出可复制的修正方案
    assert "https://api.deepseek.com" in problem
    assert "404" in problem


def test_validate_rejects_full_request_path() -> None:
    problem = make_settings(
        base_url="https://api.deepseek.com/v1/chat/completions"
    ).validate()
    assert problem is not None
    assert "base_url" in problem


def test_validate_rejects_empty_and_schemeless() -> None:
    assert make_settings(base_url="").validate() is not None
    assert make_settings(base_url="api.deepseek.com").validate() is not None


def test_normalized_base_url_strips_trailing_slash() -> None:
    assert make_settings(base_url="https://api.deepseek.com/").normalized_base_url == (
        "https://api.deepseek.com"
    )


def test_deepseek_client_fails_fast_on_bad_base_url() -> None:
    """构造客户端时就应报错，不应等到发起网络请求。"""
    from agent.llm_client import DeepSeekClient

    with pytest.raises(RuntimeError) as exc:
        DeepSeekClient(make_settings(base_url="https://api.deepseek.com/anthropic"))
    assert "Anthropic 兼容端点" in str(exc.value)


def test_deepseek_client_requires_api_key() -> None:
    from agent.llm_client import DeepSeekClient

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        DeepSeekClient(make_settings(api_key=""))


# ---------------------------------------------------------------------------
# 系统提示词日期注入
# ---------------------------------------------------------------------------


def test_render_injects_iso_date_and_weekday() -> None:
    template = "今天是 {{CURRENT_DATE}}（星期{{CURRENT_WEEKDAY}}）。"
    # 2026-09-29 是星期二
    rendered = render_system_prompt(template, date(2026, 9, 29))
    assert rendered == "今天是 2026-09-29（星期二）。"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 9, 28), "星期一"),
        (date(2026, 10, 3), "星期六"),
        (date(2026, 10, 4), "星期日"),
    ],
)
def test_weekday_mapping(day: date, expected: str) -> None:
    # 占位符只展开成单个汉字，"星期"前缀由模板提供
    assert render_system_prompt("星期{{CURRENT_WEEKDAY}}", day) == expected


def test_system_prompt_has_no_leftover_placeholder() -> None:
    prompt = make_settings().system_prompt(today=date(2026, 9, 29))
    assert "{{CURRENT_DATE}}" not in prompt
    assert "{{CURRENT_WEEKDAY}}" not in prompt
    assert "2026-09-29" in prompt
    assert "星期二" in prompt


def test_system_prompt_teaches_relative_date_resolution() -> None:
    prompt = make_settings().system_prompt(today=date(2026, 9, 29))
    assert "周末" in prompt
    assert "自行推算" in prompt
    # 明确列出"不需要问"的情形，防止模型拿日期去骚扰用户
    assert "不需要问" in prompt


def test_system_prompt_keeps_clarification_gate() -> None:
    """日期可推算，但目的地完全未知时仍然必须澄清。"""
    prompt = make_settings().system_prompt(today=date(2026, 9, 29))
    assert "ask_user_clarification" in prompt
    assert "我想去旅行" in prompt


def test_system_prompt_declares_tool_budget_and_injection_defense() -> None:
    prompt = make_settings().system_prompt(today=date(2026, 9, 29))
    assert "12" in prompt, "必须写明工具调用预算"
    assert "数据而非指令" in prompt, "必须写明提示注入防御"


def test_load_normalizes_base_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
    assert Settings.load().base_url == "https://api.deepseek.com"


# ---------------------------------------------------------------------------
# 密钥泄漏防护
# ---------------------------------------------------------------------------

# 真实 DeepSeek Key 形如 sk- + 32 位十六进制
REAL_KEY_PATTERN = re.compile(r"sk-[0-9a-fA-F]{20,}")

# 不参与扫描的目录（虚拟环境 / 缓存 / 产物）
SKIP_DIRS = {
    ".venv", ".git", "outputs", "__pycache__", ".pytest_cache",
    "node_modules", "dist", "build", ".ruff_cache",
}


def _iter_project_files():
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root) / name


def test_env_example_uses_placeholder_only() -> None:
    """`.env.example` 的设计用途就是提交到 git，绝不允许出现真实 Key。"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            assert value.startswith("sk-xxxx"), (
                f".env.example 中出现疑似真实 Key：{value[:8]}… 请改回 sk-xxxx 占位符"
            )
            return
    pytest.fail(".env.example 里缺少 DEEPSEEK_API_KEY 占位行")


def test_no_real_api_key_in_committable_files() -> None:
    """全项目扫描：除 .env（已被 gitignore）外，任何文件都不得含真实 Key。

    这是防「误把 .env 内容复制进 .env.example 再提交」这类事故的兜底网。
    """
    offenders: list[str] = []
    for path in _iter_project_files():
        if path.name == ".env":
            continue  # 真实 Key 只允许存在这里
        if path.suffix not in {".py", ".md", ".toml", ".txt", ".example", ".json", ".cfg", ".yaml", ".yml", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if REAL_KEY_PATTERN.search(text):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert not offenders, (
        f"以下文件疑似包含真实 API Key，请立即移除并轮换密钥：{offenders}"
    )
