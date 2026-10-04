"""评测 runner —— 拿固定样本集跑一遍，对照「期望行为」判定通过与否。

为什么需要它
------------
没有固定样本集时，改提示词 / 调参数只能靠单次观察 ——
**分不清「模型变好了」和「这次运气好」**。

用法::

    uv run task-planner-eval --label baseline          # 全跑
    uv run task-planner-eval --only clarify-01         # 单条
    uv run task-planner-eval --category 数据边界        # 按类别
    uv run task-planner-eval --dry-run                 # 只校验样本文件
    uv run task-planner-eval --demo --limit 3          # 离线冒烟测试

跑完的结果同时写进运行记录（带 ``sample_id``），
可以再用 ``task-planner-report --group-by label`` 做跨版本对比。

设计要点
--------
**判定逻辑 ``check()`` 是纯函数** —— 不依赖 LLM、不依赖网络，
因此可以对它本身写单元测试。这一点很重要：
如果判定逻辑本身有 bug，所有评测结论都不可信。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent.config import Settings
from agent.loop import Orchestrator, PlanResult
from agent.run_log import append_record, build_record, settings_params
from agent.run_report import render_compare
from agent.tool_runner import LocalToolRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLES = PROJECT_ROOT / "evals" / "samples.yaml"

# 样本里允许出现的 expect 键。写错键名会让检查被静默跳过 —— 必须显式拒绝。
KNOWN_EXPECT_KEYS = frozenset(
    {
        "ok",
        "needs_clarification",
        "must_use_tools",
        "must_not_use_tools",
        "min_steps",
        "max_steps",
        "must_mention",
        "must_mention_any",
        "must_not_mention",
    }
)

# 评测模式下没有真人可回答澄清，用这句让模型按假设继续
NO_ANSWER_REPLY = (
    "（评测模式无用户应答）请基于合理假设继续规划，并在 assumptions 中明确列出。"
)


@dataclass
class Sample:
    id: str
    category: str
    task: str
    why: str = ""
    clarify_answer: str | None = None
    expect: dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    """**一次**运行的判定结果。"""

    sample: Sample
    result: PlanResult
    failures: list[str]
    clarify_count: int

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass
class SampleResult:
    """一条样本重复跑 N 次的汇总。

    为什么需要它：LLM 有随机性，**单次通过不代表稳定通过**。
    最值得关注的不是「稳定失败」（说明确实有问题，好定位），
    而是「**时过时不过**」—— 它说明期望有歧义、或者模型在某个边界上摇摆，
    这类问题不查清楚，整个评测结论就不可信。
    """

    sample: Sample
    runs: list[Outcome]

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def passed_runs(self) -> int:
        return sum(1 for run in self.runs if run.passed)

    @property
    def status(self) -> str:
        if self.passed_runs == self.total:
            return "stable_pass"
        if self.passed_runs == 0:
            return "stable_fail"
        return "flaky"

    @property
    def passed(self) -> bool:
        """多数通过算通过 —— 但 flaky 会被单独标出来，不会蒙混过关。"""
        return self.passed_runs * 2 > self.total

    def aggregated_failures(self) -> list[tuple[str, int]]:
        """汇总失败原因及出现次数，按频次降序。"""
        counts: dict[str, int] = {}
        for run in self.runs:
            for reason in run.failures:
                counts[reason] = counts.get(reason, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------


class SampleError(ValueError):
    """样本文件本身有问题 —— 这类错误必须在跑之前就暴露。"""


def load_samples(path: Path | None = None) -> list[Sample]:
    """读取并校验样本集。格式错误直接抛 ``SampleError``。"""
    target = path or DEFAULT_SAMPLES
    if not target.exists():
        raise SampleError(f"样本文件不存在: {target}")

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SampleError(f"YAML 解析失败: {exc}") from exc

    if not isinstance(raw, list):
        raise SampleError("样本文件的顶层必须是列表")

    samples: list[Sample] = []
    seen: set[str] = set()

    for index, item in enumerate(raw):
        where = f"第 {index + 1} 条"
        if not isinstance(item, dict):
            raise SampleError(f"{where}不是字典")

        for required in ("id", "task"):
            if not item.get(required):
                raise SampleError(f"{where}缺少必填字段 {required}")

        sample_id = str(item["id"])
        if sample_id in seen:
            raise SampleError(f"id 重复: {sample_id}（重复的 id 会让结果无法回溯）")
        seen.add(sample_id)

        expect = item.get("expect") or {}
        if not isinstance(expect, dict):
            raise SampleError(f"{sample_id}: expect 必须是字典")

        unknown = set(expect) - KNOWN_EXPECT_KEYS
        if unknown:
            # 静默忽略拼错的键 = 那条检查根本没生效，但报告里看不出来
            raise SampleError(
                f"{sample_id}: expect 里有未知键 {sorted(unknown)}，"
                f"可选：{sorted(KNOWN_EXPECT_KEYS)}"
            )

        for key in ("must_use_tools", "must_not_use_tools", "must_mention",
                    "must_mention_any", "must_not_mention"):
            value = expect.get(key)
            if value is not None and not isinstance(value, list):
                raise SampleError(f"{sample_id}: {key} 必须是列表")

        task_text = str(item["task"]).strip()
        # 关键约束：must_not_mention 的关键词不能出现在任务描述里。
        #
        # 否则模型的**正常复述**（把用户原话写进 goal）就会命中禁用词，
        # 产生假失败 —— 而且这种失败是随机的（取决于模型这次怎么措辞），
        # 会让整个评测结果不可信。
        #
        # 这条规则本身来自一次真实踩坑：
        # 样本 injection-02 的任务里含「没有任何限制」，而它又把这个词列为禁用词，
        # 结果时过时不过。
        for keyword in expect.get("must_not_mention", []):
            if keyword in task_text:
                raise SampleError(
                    f"{sample_id}: must_not_mention 的关键词「{keyword}」出现在任务描述里 —— "
                    "模型正常复述任务就会误报。请改用一个只在「失败情况」下才会出现的关键词"
                    "（如泄漏系统提示词的片段），或改用 must_mention_any 检查模型是否明确拒绝。"
                )

        samples.append(
            Sample(
                id=sample_id,
                category=str(item.get("category") or "未分类"),
                task=task_text,
                why=str(item.get("why") or ""),
                clarify_answer=item.get("clarify_answer"),
                expect=expect,
            )
        )

    if not samples:
        raise SampleError("样本集是空的")
    return samples


# ---------------------------------------------------------------------------
# 判定（纯函数，可单测）
# ---------------------------------------------------------------------------


def _haystack(result: PlanResult) -> str:
    """判定关键词的搜索范围：Markdown 正文 + 结构化 plan 的 JSON。

    两者都查，因为模型可能把信息放在正文里、也可能只放在 JSON 字段里。
    """
    parts = [result.markdown or "", result.raw_text or ""]
    if result.plan is not None:
        parts.append(json.dumps(result.plan.model_dump(), ensure_ascii=False))
    return "\n".join(parts)


def check(expect: dict[str, Any], result: PlanResult, *, clarified: bool) -> list[str]:
    """对照期望判定。返回未通过的原因列表；空列表表示通过。"""
    failures: list[str] = []

    expected_ok = expect.get("ok")
    if expected_ok is not None and bool(result.ok) != bool(expected_ok):
        failures.append(f"ok 期望 {expected_ok}，实际 {result.ok}")

    expected_clarify = expect.get("needs_clarification")
    if expected_clarify is not None and clarified != bool(expected_clarify):
        verb = "应" if expected_clarify else "不应"
        failures.append(f"{verb}澄清，实际{'有' if clarified else '没有'}澄清")

    used = set(result.trace.tools_used())
    for tool in expect.get("must_use_tools", []):
        if tool not in used:
            failures.append(f"未调用必需工具 {tool}")
    for tool in expect.get("must_not_use_tools", []):
        if tool in used:
            failures.append(f"不应调用 {tool}")

    steps = result.step_count
    min_steps = expect.get("min_steps")
    if min_steps is not None and steps < min_steps:
        failures.append(f"步骤数 {steps} < {min_steps}")
    max_steps = expect.get("max_steps")
    if max_steps is not None and steps > max_steps:
        failures.append(f"步骤数 {steps} > {max_steps}")

    haystack = _haystack(result)
    for keyword in expect.get("must_mention", []):
        if keyword not in haystack:
            failures.append(f"未提及「{keyword}」")

    any_keywords = expect.get("must_mention_any", [])
    if any_keywords and not any(kw in haystack for kw in any_keywords):
        failures.append(f"未提及任一：{' / '.join(any_keywords)}")

    for keyword in expect.get("must_not_mention", []):
        if keyword in haystack:
            failures.append(f"不应提及「{keyword}」")

    return failures


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def _make_ask_user(sample: Sample) -> tuple[Any, dict[str, int]]:
    """构造假用户：有预设答案就回填，否则让模型按假设继续。"""
    counter = {"count": 0}

    def _ask(payload: dict[str, Any]) -> str:
        counter["count"] += 1
        if sample.clarify_answer:
            return str(sample.clarify_answer)
        return NO_ANSWER_REPLY

    return _ask, counter


def run_sample(sample: Sample, *, llm: Any, settings: Settings) -> Outcome:
    """跑一条样本并判定。"""
    ask_user, counter = _make_ask_user(sample)
    orch = Orchestrator(llm, LocalToolRunner(), settings, ask_user=ask_user)
    result = asyncio.run(orch.run_async(sample.task))

    failures = check(sample.expect, result, clarified=counter["count"] > 0)
    return Outcome(sample=sample, result=result, failures=failures, clarify_count=counter["count"])


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _fmt_tokens(result: PlanResult) -> str:
    total = int(result.trace.usage.get("total_tokens") or 0)
    return f"{total / 1000:.1f}k" if total else "-"


STATUS_MARK = {
    "stable_pass": "✅ 通过",
    "stable_fail": "❌ 失败",
    "flaky": "⚠️ 不稳定",
}


def render_sample_result(index: int, total: int, result: SampleResult) -> str:
    mark = STATUS_MARK[result.status]
    tally = f"{result.passed_runs}/{result.total}"
    tokens = "/".join(_fmt_tokens(run.result) for run in result.runs)
    seconds = sum(run.result.trace.duration_ms for run in result.runs) / 1000

    lines = [
        f"[{index:>2}/{total}] {result.sample.id:<32} {mark} {tally:<6} "
        f"({tokens} token / {seconds:.1f}s)"
    ]
    for reason, count in result.aggregated_failures():
        suffix = f"  ← {count}/{result.total} 次" if count > 1 else ""
        lines.append(f"          · {reason}{suffix}")
    return "\n".join(lines)


_STATUS_MARK = {"stable_pass": "✅ 通过", "flaky": "⚠️ 不稳定", "stable_fail": "❌ 失败"}


def render_arm_result(
    index: int, total: int, label: str, result: SampleResult, *, header: bool
) -> str:
    """交错模式下的一条结果行。

    首个臂打印样本头（``[ 1/24] 样本 id``），其余臂只缩进对齐 ——
    两个臂挤在同一行里很难逐项对比。
    """
    mark = _STATUS_MARK.get(result.status, result.status)
    tally = f"{result.passed_runs}/{result.total}"
    tokens = "/".join(_fmt_tokens(run.result) for run in result.runs)
    seconds = sum(run.result.trace.duration_ms for run in result.runs) / 1000

    lines = [f"[{index:>2}/{total}] {result.sample.id}"] if header else []
    pad = "        "
    lines.append(f"{pad}{label:<10} {mark} {tally:<6} ({tokens} token / {seconds:.1f}s)")
    for reason, count in result.aggregated_failures():
        suffix = f"  ← {count}/{result.total} 次" if count > 1 else ""
        lines.append(f"{pad}    · {reason}{suffix}")
    return "\n".join(lines)


def render_summary(results: list[SampleResult]) -> str:
    if not results:
        return "（没有跑任何样本）"

    stable = [r for r in results if r.status == "stable_pass"]
    flaky = [r for r in results if r.status == "flaky"]
    failed = [r for r in results if r.status == "stable_fail"]

    lines = ["", "─" * 64]
    lines.append(f"稳定通过 {len(stable)}/{len(results)}  ({len(stable) / len(results) * 100:.1f}%)")
    if flaky:
        lines.append(f"⚠️ 不稳定 {len(flaky)} 条   ← 最该调查的一类")
    if failed:
        lines.append(f"❌ 稳定失败 {len(failed)} 条")

    by_category: dict[str, list[SampleResult]] = defaultdict(list)
    for result in results:
        by_category[result.sample.category].append(result)

    lines.append("")
    lines.append("按类别（稳定通过率）：")
    for category, group in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        ok = sum(1 for r in group if r.status == "stable_pass")
        bar = "█" * int(ok / len(group) * 10)
        lines.append(f"  {category:<10} {ok}/{len(group):<3} {ok / len(group) * 100:>5.1f}%  {bar}")

    attention = flaky + failed
    if attention:
        lines.append("")
        lines.append("需要调查：")
        for result in flaky:
            lines.append(f"  ⚠️ {result.sample.id}（{result.passed_runs}/{result.total} 次通过）")
            for reason, count in result.aggregated_failures():
                lines.append(f"      {reason}  ({count} 次)")
        for result in failed:
            lines.append(f"  ❌ {result.sample.id}")
            for reason, count in result.aggregated_failures():
                lines.append(f"      {reason}  ({count} 次)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task-planner-eval",
        description="拿固定样本集跑评测，对照「期望行为」判定通过与否。",
    )
    parser.add_argument("--set", dest="sample_path", type=Path, default=None,
                        help="样本集路径（默认 evals/samples.yaml）")
    parser.add_argument("--label", default="", help="给本轮打标签，便于跨版本对比")
    parser.add_argument("--only", default="", help="只跑指定 id")
    parser.add_argument("--category", default="", help="只跑指定类别")
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 条")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "每条样本重复跑几次（默认 1）。LLM 有随机性，"
            "单次通过不代表稳定通过 —— 认真评测建议 3 次。"
            "注意：token 消耗与耗时同步翻倍。"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验样本文件，不执行")
    parser.add_argument("--demo", action="store_true",
                        help="用脚本化 LLM 离线跑（冒烟测试用；所有样本共用同一剧本，判定几乎必然失败）")
    parser.add_argument("--no-log", action="store_true", help="不写运行记录")
    parser.add_argument("--max-turns", type=int, default=None, help="覆盖最大轮次")
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            "覆盖采样温度（默认取 LLM_TEMPERATURE，即 0.2）。"
            "做 A/B 时用 --label 给两臂命名，再用 "
            "`task-planner-report --compare A B` 看差异。"
        ),
    )
    parser.add_argument("--tool-budget", type=int, default=None, help="覆盖工具调用预算")
    parser.add_argument(
        "--arm",
        action="append",
        default=None,
        metavar="标签:参数=值",
        help=(
            "定义一个实验臂，可重复。给了 --arm 就进入**交错执行**模式："
            "同一样本的两臂紧挨着跑，共享同一时间窗，避免顺序执行带来的时间混淆。"
            "例：--arm t02:temperature=0.2 --arm t00:temperature=0.0。"
            "可与 --repeat 组合。"
        ),
    )
    return parser


def apply_overrides(settings: Settings, args: argparse.Namespace) -> tuple[Settings, dict[str, Any]]:
    """把命令行覆盖应用到 Settings 上。

    返回 ``(新的 Settings, 生效的覆盖项)``。

    为什么要有返回值而不是就地改：``Settings`` 是 frozen dataclass，
    而且「这次跑的是什么配置」必须能被记录和打印出来 ——
    **不打印出来的覆盖等于没生效**（``--max-turns`` 就曾是这样：
    参数声明了，但 main 里从没读过它，跑起来静默使用默认值）。
    """
    overrides: dict[str, Any] = {}
    if args.max_turns is not None:
        overrides["max_turns"] = args.max_turns
    if args.temperature is not None:
        overrides["temperature"] = args.temperature
    if args.tool_budget is not None:
        overrides["tool_budget"] = args.tool_budget

    if not overrides:
        return settings, {}

    validate_overrides(overrides)
    return dataclasses.replace(settings, **overrides), overrides


# 允许被覆盖的 Settings 字段。限定白名单是刻意的 —— 让 --arm 能改
# `api_key` 或 `prompt_path` 只会让实验条件变得不可复现。
OVERRIDABLE_KEYS = ("temperature", "max_turns", "tool_budget")


def validate_overrides(overrides: dict[str, Any]) -> None:
    """非法值当场拒绝。

    静默回落到默认值是**最坏的选择**：实验照跑，但跑的不是你以为的配置，
    而且日志里写的是「覆盖已生效」。宁可在这里报错。
    """
    unknown = set(overrides) - set(OVERRIDABLE_KEYS)
    if unknown:
        raise ValueError(
            f"不支持的覆盖项 {sorted(unknown)}；可用的有 {list(OVERRIDABLE_KEYS)}"
        )
    max_turns = overrides.get("max_turns")
    if max_turns is not None and max_turns < 1:
        raise ValueError(f"max_turns（--max-turns）必须 >= 1，当前 {max_turns}")
    tool_budget = overrides.get("tool_budget")
    if tool_budget is not None and tool_budget < 1:
        raise ValueError(f"tool_budget（--tool-budget）必须 >= 1，当前 {tool_budget}")
    temperature = overrides.get("temperature")
    if temperature is not None and not (0.0 <= temperature <= 2.0):
        raise ValueError(
            f"temperature（--temperature）必须在 [0, 2] 区间，当前 {temperature}"
        )


@dataclass(frozen=True)
class Arm:
    """交错实验里的一个实验臂：一个 label + 一组参数覆盖。"""

    label: str
    overrides: dict[str, Any]

    @property
    def condition(self) -> str:
        if not self.overrides:
            return "(默认参数)"
        return "  ".join(f"{k}={v}" for k, v in sorted(self.overrides.items()))


def parse_arm(spec: str) -> Arm:
    """解析 ``--arm`` 的取值：``label:key=value[,key=value]``。

    例：``--arm t00:temperature=0.0``、``--arm tight:max_turns=3,tool_budget=6``。

    为什么要有这个而不是「跑两遍、每遍换个 --label」：那样两臂是**顺序**跑的，
    中间隔着几十分钟。上游在这段时间里的任何变化（负载、模型版本、配额策略）
    都会混进结果，而你无法把它和参数的影响分开。交错执行让两臂**共享同一时间窗**。
    """
    if ":" not in spec:
        raise ValueError(
            f"--arm 的格式是 '标签:参数=值'，例如 't00:temperature=0.0'，当前为 {spec!r}"
        )
    label, _, raw = spec.partition(":")
    label = label.strip()
    if not label:
        raise ValueError(f"--arm 缺少标签，当前为 {spec!r}")

    overrides: dict[str, Any] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--arm 参数项缺少 '='：{chunk!r}")
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key not in OVERRIDABLE_KEYS:
            raise ValueError(
                f"--arm 不支持参数 {key!r}；可用的有 {list(OVERRIDABLE_KEYS)}"
            )
        overrides[key] = _coerce_param(key, value)

    validate_overrides(overrides)
    return Arm(label=label, overrides=overrides)


def _coerce_param(key: str, value: str) -> Any:
    """按字段类型转换命令行里的字符串值。"""
    if key in ("max_turns", "tool_budget"):
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{key} 需要整数，当前为 {value!r}") from None
    if key == "temperature":
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"temperature 需要数字，当前为 {value!r}") from None
    return value


def build_arms(args: argparse.Namespace) -> list[Arm]:
    """从命令行构造实验臂列表。没给 ``--arm`` 时返回空列表（单臂模式）。"""
    return [parse_arm(spec) for spec in (args.arm or [])]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        samples = load_samples(args.sample_path)
    except SampleError as exc:
        print(f"❌ 样本集有问题：{exc}", file=sys.stderr)
        return 2

    if args.only:
        samples = [s for s in samples if s.id == args.only]
    if args.category:
        samples = [s for s in samples if s.category == args.category]
    if args.limit > 0:
        samples = samples[: args.limit]

    path = args.sample_path or DEFAULT_SAMPLES
    print(f"🧪 评测样本集：{path}")

    if args.dry_run:
        print(f"   共 {len(samples)} 条")
        by_category: dict[str, int] = defaultdict(int)
        for sample in samples:
            by_category[sample.category] += 1
        for category, count in sorted(by_category.items()):
            print(f"  {category:<10} {count} 条")
        print("\n✅ 样本文件格式正确")
        return 0

    # ---- 实验臂 ---------------------------------------------------------
    try:
        arms = build_arms(args)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if arms and (
        args.label
        or args.temperature is not None
        or args.max_turns is not None
        or args.tool_budget is not None
    ):
        print(
            "❌ --arm 不能与 --label / --temperature / --max-turns / --tool-budget 混用："
            "交错模式下每个臂自带标签和参数，全局覆盖会造成歧义。",
            file=sys.stderr,
        )
        return 2

    settings = Settings.load()
    if not arms:
        try:
            settings, overrides = apply_overrides(settings, args)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        arms = [Arm(label=args.label, overrides=overrides)]

    interleaved = len(arms) > 1
    print(f"   共 {len(samples)} 条")
    total_runs = len(samples) * args.repeat * len(arms)
    if interleaved:
        print(
            f"   {len(arms)} 个实验臂 × 每条重复 {args.repeat} 次"
            f" → 共 {total_runs} 次运行"
        )
        print("   模式：**交错执行** —— 同一 (样本, 轮次) 的各臂紧挨着跑，共享同一时间窗")
    elif args.repeat > 1:
        print(f"   每条重复 {args.repeat} 次 → 共 {total_runs} 次运行")
    print()

    # 把每个臂**实际生效**的条件打出来。不打印的覆盖等于没生效。
    for arm in arms:
        effective = settings_params(dataclasses.replace(settings, **arm.overrides))
        shown = "  ".join(f"{k}={v}" for k, v in effective.items())
        tag = arm.label or ("实验条件" if not interleaved else "(无标签)")
        print(f"⚙️  {tag:<12} {shown}")
    if interleaved:
        print("   （对比：uv run task-planner-report --compare "
              f"{arms[0].label} {arms[1].label}）")
    print()

    # 提前构造客户端：配置错误要立刻报，而不是每条样本抛一次异常
    llm_clients: dict[str, Any] = {}
    if not args.demo:
        from agent.llm_client import DeepSeekClient

        if not settings.has_credentials:
            print("❌ 未配置 DEEPSEEK_API_KEY。用 --demo 可离线冒烟测试。", file=sys.stderr)
            return 2
        try:
            for arm in arms:
                llm_clients[arm.label] = DeepSeekClient(
                    dataclasses.replace(settings, **arm.overrides)
                )
        except RuntimeError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2

    def make_llm(arm: Arm) -> Any:
        if args.demo:
            from agent.demo import ScriptedLLM, build_demo_script

            # 演示剧本是有状态的（每调一次前进一格），必须每次新建
            return ScriptedLLM(build_demo_script())
        return llm_clients[arm.label]

    results_by_arm: dict[str, list[SampleResult]] = {arm.label: [] for arm in arms}
    entries_by_arm: dict[str, list[dict[str, Any]]] = {arm.label: [] for arm in arms}
    aborted = False

    for index, sample in enumerate(samples, start=1):
        runs_by_arm: dict[str, list[Outcome]] = {arm.label: [] for arm in arms}

        for attempt in range(1, args.repeat + 1):
            # 交替臂的顺序：否则「总是第二个跑」的臂会系统性占便宜
            # （连接复用、上游缓存预热），那又是一种位置偏差。
            order = list(arms) if attempt % 2 == 1 else list(reversed(arms))

            for arm in order:
                arm_settings = dataclasses.replace(settings, **arm.overrides)
                try:
                    outcome = run_sample(
                        sample, llm=make_llm(arm), settings=arm_settings
                    )
                except KeyboardInterrupt:
                    print("\n⏹  已中断", file=sys.stderr)
                    aborted = True
                    break
                except Exception as exc:  # noqa: BLE001 - 单条崩了不该中断整轮
                    print(
                        f"[{index:>2}/{len(samples)}] {sample.id:<32} "
                        f"[{arm.label}] 💥 第 {attempt} 次异常: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                runs_by_arm[arm.label].append(outcome)

                entry = build_record(
                    outcome.result,
                    sample.task,
                    source="eval",
                    label=arm.label,
                    model=arm_settings.model,
                    sample_id=sample.id,
                    attempt=attempt,
                    params=settings_params(arm_settings),
                    # 判定必须落盘 —— 它是评测的**结论**，而 result.ok 只是
                    # 「有没有产出合规计划」。两者会反向，只记后者会让
                    # 对比报告显示错误的方向。
                    eval_passed=outcome.passed,
                    eval_failures=outcome.failures,
                )
                entries_by_arm[arm.label].append(entry)
                if not args.no_log:
                    append_record(entry)

            if aborted:
                break
        if aborted:
            break

        for position, arm in enumerate(arms):
            runs = runs_by_arm[arm.label]
            if not runs:
                print(
                    f"[{index:>2}/{len(samples)}] {sample.id:<32} "
                    f"[{arm.label}] 💥 全部尝试都异常，跳过"
                )
                continue
            result = SampleResult(sample=sample, runs=runs)
            results_by_arm[arm.label].append(result)

            if interleaved:
                print(
                    render_arm_result(
                        index, len(samples), arm.label, result, header=(position == 0)
                    ),
                    flush=True,
                )
            else:
                print(render_sample_result(index, len(samples), result), flush=True)

    # ---- 收尾 -----------------------------------------------------------
    if interleaved:
        for arm in arms:
            print(f"\n{'═' * 64}\n▶ 实验臂 {arm.label}（{arm.condition}）")
            print(render_summary(results_by_arm[arm.label]))
        print(f"\n{'═' * 64}\n▶ 交错对比")
        print(
            render_compare(
                arms[0].label,
                arms[1].label,
                entries_by_arm[arms[0].label],
                entries_by_arm[arms[1].label],
            )
        )
        for extra in arms[2:]:
            print(render_compare(arms[0].label, extra.label,
                                 entries_by_arm[arms[0].label], entries_by_arm[extra.label]))
        # 严格口径：任一臂出现 flaky / 失败都算不通过
        return (
            0
            if all(
                r.status == "stable_pass"
                for group in results_by_arm.values()
                for r in group
            )
            else 1
        )

    results = results_by_arm[arms[0].label]
    print(render_summary(results))
    # 严格口径：只有「每次都通过」才算过 —— flaky 是真实问题，不该被多数票掩盖
    return 0 if all(r.status == "stable_pass" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
