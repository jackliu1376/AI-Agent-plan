"""命令行入口。

用法::

    # 真实调用 DeepSeek
    uv run task-planner "我想周末去成都玩两天，带小孩，人均预算 1000"

    # 走 MCP 协议调用工具（验证 BYOA 的 Context Integration 要求）
    uv run task-planner "帮我规划杭州 3 日游" --transport mcp

    # 没有 API Key 也能跑通全链路的离线演示
    uv run task-planner --demo

    # 只看工具清单
    uv run task-planner --list-tools
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from agent.config import Settings
from agent.demo import ScriptedLLM, build_demo_script
from agent.loop import Orchestrator
from agent.tool_runner import LocalToolRunner, MCPToolRunner, registered_tool_names
from common.console import ensure_safe_stdio


def _make_ask_user(interactive: bool):
    """构造澄清回调。非交互环境下返回空答案，让模型走假设分支。"""

    def ask(payload: dict[str, Any]) -> str:
        question = payload.get("question", "")
        options = payload.get("options") or []
        reason = payload.get("reason", "")
        print("\n" + "=" * 60)
        print("🤔 需要你补充信息：")
        if reason:
            print(f"   原因：{reason}")
        print(f"   {question}")
        for idx, opt in enumerate(options, 1):
            print(f"   [{idx}] {opt}")
        if not interactive:
            print("   （非交互环境，跳过）")
            return ""
        try:
            answer = input("> ").strip()
        except EOFError:
            return ""
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer

    return ask


def _error_hint(exc: BaseException) -> str:
    """把常见异常翻译成可操作的排查提示。"""
    name = type(exc).__name__.lower()
    text = str(exc).lower()

    if "401" in text or "authentication" in text or "invalid api key" in text:
        return "检查 DEEPSEEK_API_KEY 是否正确、是否已过期。"
    if "404" in text:
        return (
            "检查 DEEPSEEK_BASE_URL —— DeepSeek 的 Anthropic 兼容端点会 404，"
            "应使用 https://api.deepseek.com"
        )
    if "429" in text or "rate limit" in text:
        return "被上游限流。稍后重试，或降低并发。"
    if "timeout" in name or "timeout" in text:
        return "上游超时。可调大 LLM_TIMEOUT，或稍后重试。"
    if "connect" in name or "connection" in text:
        return "网络不可达。检查网络连接或代理设置。"
    return ""


def _print_result(result) -> int:  # noqa: ANN001
    print("\n" + "=" * 60)
    print("📋 计划（人类可读版）")
    print("=" * 60)
    print(result.markdown or "(模型未输出 Markdown 部分)")

    if result.plan is not None:
        print("\n" + "=" * 60)
        print("🧩 计划（结构化摘要）")
        print("=" * 60)
        print(f"目标：{result.plan.goal}")
        if result.plan.assumptions:
            print("假设：")
            for a in result.plan.assumptions:
                print(f"  - {a}")

        for phase in result.plan.phases:
            print(f"\n【{phase.name}】")
            for step in phase.steps:
                dep = f" ← {','.join(step.depends_on)}" if step.depends_on else ""
                tool = f" [工具:{step.tool}]" if step.tool else ""
                print(f"  {step.id}. {step.action}{tool}{dep}")
        print(f"\n总耗时：{result.plan.total_eta}｜预算：{result.plan.budget_estimate}")
        dag_ok, dag_msg = result.plan.check_dag()
        print(f"依赖图校验：{'✅ 无环' if dag_ok else '❌ ' + dag_msg}")
        print(f"引用工具：{', '.join(sorted(result.plan.referenced_tools())) or '（无）'}")

        # 数据时效：让用户知道这些数字有多新
        if result.plan.data_freshness or result.plan.verification_channels:
            print("\n⏱  数据时效")
            if result.plan.data_freshness:
                print(f"  {result.plan.data_freshness}")
            for channel in result.plan.verification_channels:
                print(f"  · 出行前可核实：{channel}")

    print("\n" + "=" * 60)
    print("🔍 运行轨迹")
    print("=" * 60)
    print(result.summary())
    for record in result.trace.tool_calls:
        flag = "✅" if record.ok else f"❌ {record.error_code}"
        cached = " (缓存)" if record.cached else ""
        print(f"  {flag} {record.tool}{cached} {record.latency_ms}ms")
    if result.trace.injection_flags:
        print(f"  ⚠️ 提示注入特征命中：{len(result.trace.injection_flags)} 条")
    if result.trace.usage:
        print(f"  Token 用量：{result.trace.usage}")

    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台是 GBK，emoji 编码不了会抛 UnicodeEncodeError。
    # 这个文件里有 18 处 emoji，不兜底的话在真实控制台里会直接崩。
    ensure_safe_stdio()

    parser = argparse.ArgumentParser(
        prog="task-planner",
        description="任务规划助手 —— 把复杂任务分解为有序可执行的步骤",
    )
    parser.add_argument("task", nargs="?", help="要规划的任务描述")
    parser.add_argument(
        "--transport",
        choices=["local", "mcp"],
        default="local",
        help="工具调用通道：local=进程内直调，mcp=通过 MCP 协议（默认 local）",
    )
    parser.add_argument("--demo", action="store_true", help="离线演示：不调用真实 LLM")
    parser.add_argument("--list-tools", action="store_true", help="打印工具清单后退出")
    parser.add_argument("--save", metavar="FILE", help="把 Markdown 计划保存到 outputs/<FILE>")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    parser.add_argument("--max-turns", type=int, default=None, help="覆盖最大轮次")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="以事件流方式输出进度（验证流式接口；Web 层走同一套事件）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印编排过程日志")
    parser.add_argument(
        "--label",
        default="",
        help="给本次运行打标签，便于事后用 --group-by label 对比（如 prompt-v2）",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="不写入运行记录（默认写入 logs/runs.jsonl）",
    )
    args = parser.parse_args(argv)

    if args.list_tools:
        print("已注册工具：")
        for name in registered_tool_names():
            print(f"  - {name}")
        return 0

    if not args.task and not args.demo:
        parser.print_help()
        print("\n提示：先试 `uv run task-planner --demo` 看看完整效果。")
        return 2

    settings = Settings.load()

    # ---- 选择 LLM ----
    if args.demo:
        llm = ScriptedLLM(build_demo_script())
        task = args.task or "我想周末去成都玩两天，带小孩，人均预算 1000"
        print(f"🧪 离线演示模式（脚本化 LLM），任务：{task}")
    else:
        if not settings.has_credentials:
            print(
                "❌ 未配置 DEEPSEEK_API_KEY。\n"
                "   请执行：cp .env.example .env 并填入真实 Key；\n"
                "   或改用离线演示：uv run task-planner --demo",
                file=sys.stderr,
            )
            return 2
        from agent.llm_client import DeepSeekClient

        llm = DeepSeekClient(settings)
        task = args.task or ""

    ask_user = _make_ask_user(interactive=not args.demo and sys.stdin.isatty())

    # ---- 选择工具通道 ----
    def _on_event(event) -> None:  # noqa: ANN001
        icons = {
            "run_started": "▶",
            "turn_started": "◆",
            "tool_call": "→",
            "clarification": "?",
            "repair": "!",
            "plan_ready": "✔",
        }
        prefix = icons.get(event.type, " ")
        print(f"  {prefix} {event.summary()}")

    stream_cb = _on_event if args.stream else None

    try:
        if args.transport == "mcp":
            import asyncio

            async def _run_mcp():
                async with MCPToolRunner() as runner:
                    names = await runner.list_remote_tools()
                    print(
                        f"🔌 已通过 MCP 协议发现 {len(names)} 个工具："
                        f"{', '.join(sorted(names))}"
                    )
                    orch = Orchestrator(
                        llm, runner, settings, ask_user, verbose=args.verbose
                    )
                    return await orch.run_async(
                        task, max_turns=args.max_turns, on_event=stream_cb
                    )

            result = asyncio.run(_run_mcp())
        else:
            import asyncio

            orch = Orchestrator(
                llm, LocalToolRunner(), settings, ask_user, verbose=args.verbose
            )
            result = asyncio.run(
                orch.run_async(task, max_turns=args.max_turns, on_event=stream_cb)
            )
    except KeyboardInterrupt:
        print("\n⏹  已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底：给人话，不吐 traceback
        print(f"\n❌ 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        hint = _error_hint(exc)
        if hint:
            print(f"   {hint}", file=sys.stderr)
        return 1

    code = _print_result(result)

    if args.json and result.plan is not None:
        print("\n" + json.dumps(result.plan.model_dump(), ensure_ascii=False, indent=2))

    # 运行记录：把这次运行落成一行 JSONL，供事后汇总对比
    if not args.no_log:
        from agent.run_log import record_run, settings_params

        entry = record_run(
            result,
            task,
            source="demo" if args.demo else "cli",
            label=args.label,
            model=settings.model,
            params=settings_params(settings),
        )
        if entry is not None:
            print(f"\n📝 已记录运行 {entry['run_id']} → {entry['output_status']}")

    if args.save:
        from mcp_server.tools.base import invoke

        content = result.markdown or result.raw_text
        saved = invoke("save_itinerary", {"path": args.save, "content": content})
        if saved.ok:
            print(f"\n💾 已保存到 {saved.data['path']}")
        else:
            print(f"\n❌ 保存失败：{saved.error.message}", file=sys.stderr)

    return code


if __name__ == "__main__":
    raise SystemExit(main())
