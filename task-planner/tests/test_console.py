"""控制台编码兜底的测试。

背景
----
Windows 控制台默认 GBK（cp936）。`print` 一个编码不了的字符会**直接抛异常**：

    UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f680'

在 CLI 里只是难看的报错，但在**服务入口**里是致命的 ——
`uv run task-planner-web` 直接退出，用户看到的是一个起不来的服务。

这个 bug 的隐蔽之处：**开发时输出被重定向到文件或管道（编码往往是 UTF-8），
一切正常**；只有在真实控制台里跑才炸。所以必须有测试守着。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from common.console import ensure_safe_stdio

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 先证明这个测试是有意义的
# ---------------------------------------------------------------------------


def test_gbk_stream_really_does_reject_emoji() -> None:
    """GBK 流确实会拒绝 emoji —— 否则下面的测试就是空转。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    with pytest.raises(UnicodeEncodeError):
        stream.write("🚀")


def test_gbk_stream_accepts_chinese() -> None:
    """中文本身在 GBK 里是合法的 —— 所以兜底后只有 emoji 降级，中文照常显示。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    stream.write("任务规划助手")
    stream.flush()
    assert raw.getvalue().decode("gbk") == "任务规划助手"


# ---------------------------------------------------------------------------
# 兜底本身
# ---------------------------------------------------------------------------


def test_ensure_safe_stdio_survives_emoji_on_gbk(monkeypatch: pytest.MonkeyPatch) -> None:
    """加上兜底后，GBK 控制台里 print emoji 不再抛异常。"""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    ensure_safe_stdio()
    print("🚀 Cairn · 任务规划助手 API → http://127.0.0.1:8000")
    print("✅ 中文正常显示")
    stream.flush()

    out = raw.getvalue().decode("gbk", errors="replace")
    assert "Cairn" in out
    assert "任务规划助手" in out, "中文必须完整保留 —— 降级的只该是 emoji"
    assert "🚀" not in out, "emoji 应当降级，而不是让整行消失"


def test_ensure_safe_stdio_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复调用不该出错 —— 入口之间可能互相 import。"""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    ensure_safe_stdio()
    ensure_safe_stdio()
    ensure_safe_stdio()


def test_ensure_safe_stdio_tolerates_replaced_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 被换成非 TextIOWrapper（如 pytest 捕获、io.StringIO）时不能炸。"""

    class Bare:
        def write(self, _text: str) -> int:
            return 0

    monkeypatch.setattr(sys, "stdout", Bare())
    monkeypatch.setattr(sys, "stderr", Bare())

    ensure_safe_stdio()  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 入口必须真的接上
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "web/serve.py",          # 服务入口 —— 崩了就是服务起不来
        "agent/cli.py",          # 18 处 emoji
        "desktop/app.py",
    ],
)
def test_entry_points_call_ensure_safe_stdio(entry: str) -> None:
    """入口必须在 print 之前调用兜底。

    这条测试防的是「改了新入口但忘了接上」—— 那种情况下 bug 只会在
    真实控制台里复现，CI 里永远看不到。
    """
    source = (PROJECT_ROOT / entry).read_text(encoding="utf-8")
    assert "ensure_safe_stdio()" in source, f"{entry} 没有调用 ensure_safe_stdio()"


def test_mcp_server_stdout_is_untouched() -> None:
    """MCP server 的 stdout 是 JSON-RPC 协议通道，绝不能往里加东西。"""
    source = (PROJECT_ROOT / "mcp_server" / "server.py").read_text(encoding="utf-8")
    assert "ensure_safe_stdio" not in source, (
        "MCP server 的 stdout 承载 JSON-RPC，不要动它"
    )
