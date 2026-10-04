"""运行记录汇总报告 —— 把 JSONL 变成可比较的指标。

用法::

    uv run task-planner-report                          # 全部记录的汇总
    uv run task-planner-report --last 20                # 只看最近 20 次
    uv run task-planner-report --group-by label         # 按 label 分组对比
    uv run task-planner-report --group-by params.temperature   # 按实际参数分组
    uv run task-planner-report --compare exp-t02 exp-t00       # A/B 并排对比
    uv run task-planner-report --json                   # 机器可读，便于画图

典型场景：改了提示词 / 调了参数后，用 ``--label`` 跑一批样本，
再 ``--compare`` 就能看出指标涨了还是跌了 ——
这是「调优」和「碰运气」的分界线。

对比表会额外报告 **不稳定样本数（flaky）**：同一样本重复跑结果不一致。
它比「失败」更值得调查 —— 稳定失败说明功能坏了（方向明确），
flaky 说明结论依赖运气，会让你把噪声当成改进。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agent.run_log import load_runs, log_path

BAR = "─" * 60


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def _safe_mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else 0.0


def _verdict(record: dict[str, Any]) -> bool:
    """这条记录**算不算通过**。

    优先用评测判定 ``eval_passed``；没有时（CLI / Web 来源）退回 ``ok``。

    为什么必须优先：``ok`` 只表示「产出了合规计划」，而评测判定表示
    「满足样本声明的期望行为」，**两者会反向**。实测踩坑 —— 样本
    ``injection-01`` 的期望里没有 ``ok`` 字段，于是：

    - 模型拒绝且没产出计划 → ``ok=False``，但**判定通过**
    - 模型产出了合规计划但正文提到禁用词 → ``ok=True``，但**判定失败**

    只用 ``ok`` 的话，对比报告会把结论显示反（当时说 temperature=0 更差，
    实际是更好）。
    """
    passed = record.get("eval_passed")
    if passed is None:
        return bool(record.get("ok"))
    return bool(passed)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把一组记录压成一份统计摘要。

    ``success_rate`` 走**判定口径**（``_verdict``）—— 这才是「这个改动好不好」
    该看的数字。``plan_ok_rate`` 是原始口径（合规计划产出率），保留下来是因为
    「计划能不能解析」本身也是有价值的信号。
    """
    if not records:
        return {"count": 0}

    ok_count = sum(1 for r in records if r.get("ok"))
    verdict_count = sum(1 for r in records if _verdict(r))
    tool_calls = [c for r in records for c in r.get("tool_calls", [])]
    failed_calls = [c for c in tool_calls if not c.get("ok")]

    tokens = [
        int(r.get("usage", {}).get("total_tokens") or 0)
        for r in records
    ]
    durations = [int(r.get("duration_ms") or 0) for r in records]

    by_tool: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "failed": 0, "latency": []}
    )
    for call in tool_calls:
        bucket = by_tool[call.get("tool", "?")]
        bucket["count"] += 1
        if not call.get("ok"):
            bucket["failed"] += 1
        bucket["latency"].append(int(call.get("latency_ms") or 0))

    return {
        "count": len(records),
        "ok_count": ok_count,
        "plan_ok_rate": round(ok_count / len(records) * 100, 1),
        "verdict_count": verdict_count,
        "success_rate": round(verdict_count / len(records) * 100, 1),
        "evaluated_count": sum(
            1 for r in records if r.get("eval_passed") is not None
        ),
        "output_status": dict(Counter(r.get("output_status", "?") for r in records)),
        "stop_reason": dict(Counter(r.get("stop_reason", "?") for r in records)),
        "avg_turns": _safe_mean([r.get("turns", 0) for r in records]),
        "max_turns": max((r.get("turns", 0) for r in records), default=0),
        "avg_repairs": _safe_mean([r.get("repairs", 0) for r in records]),
        "repair_rate": round(
            sum(1 for r in records if r.get("repairs", 0) > 0) / len(records) * 100, 1
        ),
        "avg_tool_calls": _safe_mean([r.get("tool_call_count", 0) for r in records]),
        "tool_failure_rate": (
            round(len(failed_calls) / len(tool_calls) * 100, 1) if tool_calls else 0.0
        ),
        "avg_duration_ms": _safe_mean(durations),
        "avg_tokens": _safe_mean(tokens),
        "total_tokens": sum(tokens),
        "avg_steps": _safe_mean([r.get("step_count", 0) for r in records]),
        "dag_failure_count": sum(1 for r in records if r.get("dag_ok") is False),
        "error_codes": dict(
            Counter(c["error_code"] for c in failed_calls if c.get("error_code"))
        ),
        "tools": {
            name: {
                "count": bucket["count"],
                "failed": bucket["failed"],
                "failure_rate": round(bucket["failed"] / bucket["count"] * 100, 1),
                "avg_latency_ms": _safe_mean(bucket["latency"]),
            }
            for name, bucket in sorted(
                by_tool.items(), key=lambda kv: -kv[1]["count"]
            )
        },
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_ms(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _distribution(counter: dict[str, int]) -> str:
    if not counter:
        return "（无）"
    return "  ".join(f"{k}={v}" for k, v in sorted(counter.items(), key=lambda kv: -kv[1]))


def _dig(record: dict[str, Any], path: str) -> Any:
    """按点号路径取值：``params.temperature`` → ``record["params"]["temperature"]``。

    支持嵌套路径是为了能直接按**实验条件**分组（而不是按 label）。
    老记录没有 ``params`` 字段，这里会返回 None 并归入「(未标注)」。
    """
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def stability(records: list[dict[str, Any]]) -> dict[str, Any]:
    """按 sample_id 聚合，区分「稳定通过 / 稳定失败 / 时过时不过」。

    **flaky 比 failing 更值得调查。** 稳定失败说明方向明确（功能坏了）；
    flaky 说明结论依赖运气 —— 它会让你在 A/B 对比时把噪声当成改进。
    所以这里单独把它拎出来计数。

    另外统计 **计划指纹的唯一性**：同一样本重复跑，若每次产出的
    ``plan_digest`` 都不同，说明模型每次走的路径都不一样 —— 即使判定全过，
    计划本身也在漂移。这是「确定性」的直接度量（温度实验的核心指标）。

    注意用的是**判定口径**（``_verdict``）而不是 ``result.ok`` —— 后者会把
    「产出了合规计划但违反了期望行为」误算成通过。
    """
    by_sample: dict[str, list[bool]] = defaultdict(list)
    digests: dict[str, set[str]] = defaultdict(set)
    for record in records:
        sample_id = str(record.get("sample_id") or "")
        if not sample_id:
            continue
        by_sample[sample_id].append(_verdict(record))
        digest = record.get("plan_digest")
        if digest:
            digests[sample_id].add(str(digest))

    flaky = sorted(s for s, results in by_sample.items() if any(results) and not all(results))
    failed = sorted(s for s, results in by_sample.items() if not any(results))
    total = len(by_sample)
    # 只对「重复跑过不止一次」的样本算指纹多样性 —— 单次运行必然只有 1 个指纹，
    # 混进来会把平均值拉低，看起来像「很确定」，其实是没测。
    repeated = [s for s, results in by_sample.items() if len(results) > 1]
    variety = [len(digests[s]) for s in repeated if digests.get(s)]

    return {
        "samples": total,
        "stable_pass": sum(1 for r in by_sample.values() if all(r)),
        "stable_fail": len(failed),
        "flaky": len(flaky),
        "flaky_rate": round(len(flaky) / total * 100, 1) if total else 0.0,
        "flaky_ids": flaky,
        "failed_ids": failed,
        "repeated_samples": len(repeated),
        "avg_digest_variety": _safe_mean([float(v) for v in variety]),
        "deterministic_samples": sum(1 for v in variety if v == 1),
    }


# 对比表的一行：(显示名, 指标键, 格式化方式, 方向, 变化量口径)
#
# 方向用于给变化量打 ✅/⚠️ —— 「token 变少」和「成功率变少」含义完全相反，
# 不能用同一个箭头。
#
# 口径必须显式指定，不能从「格式化方式」推：
# 成功率用百分点（pp），平均 token 用百分比，而「不稳定样本数」这类
# **绝对计数**必须用绝对差 —— 否则基线为 0 时百分比算不出来，
# 「从 0 涨到 2」会被显示成「—」，最该看见的恶化反而被藏起来。
_COMPARE_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    ("运行次数", "runs", "int", "neutral", "abs"),
    ("通过率（判定）", "success_rate", "pct", "up", "pp"),
    ("合规计划产出率", "plan_ok_rate", "pct", "up", "pp"),
    ("不稳定样本", "flaky", "int", "down", "abs"),
    ("平均轮次", "avg_turns", "float", "down", "pct"),
    ("修复率", "repair_rate", "pct", "down", "pp"),
    ("平均 token", "avg_tokens", "int", "down", "pct"),
    ("工具失败率", "tool_failure_rate", "pct", "down", "pp"),
    ("平均耗时", "avg_duration_ms", "ms", "down", "pct"),
    ("平均步骤数", "avg_steps", "float", "neutral", "pct"),
    ("计划指纹多样性", "digest_variety", "float", "down", "pct"),
    ("依赖图不合格", "dag_failures", "int", "down", "abs"),
)


def compare_metrics(records: list[dict[str, Any]]) -> dict[str, float]:
    """把一组记录压成对比表需要的扁平指标。"""
    summary = summarize(records)
    stab = stability(records)
    return {
        "runs": summary["count"],
        "success_rate": summary["success_rate"],
        "plan_ok_rate": summary["plan_ok_rate"],
        "flaky": stab["flaky"],
        "avg_turns": summary["avg_turns"],
        "repair_rate": summary["repair_rate"],
        "avg_tokens": summary["avg_tokens"],
        "tool_failure_rate": summary["tool_failure_rate"],
        "avg_duration_ms": summary["avg_duration_ms"],
        "avg_steps": summary["avg_steps"],
        "digest_variety": stab["avg_digest_variety"],
        "dag_failures": summary["dag_failure_count"],
    }


def _fmt_metric(value: Any, kind: str) -> str:
    if kind == "pct":
        return f"{value:.1f}%"
    if kind == "int":
        return f"{int(value):,}"
    if kind == "ms":
        return _fmt_ms(value)
    return f"{float(value):.2f}"


def _fmt_delta(current: Any, baseline: Any, style: str, direction: str) -> str:
    """变化量 + 方向标记。``direction`` 决定「变大」是好是坏。"""
    try:
        cur, base = float(current), float(baseline)
    except (TypeError, ValueError):
        return "—"

    diff = cur - base

    if style == "abs":
        # 绝对计数：基线为 0 时百分比无意义，直接给绝对差
        text = f"{diff:+.0f}" if diff else "0"
        if abs(diff) < 1e-9:
            return f"{text}   ="
        if direction == "neutral":
            return text
        improved = (diff > 0) if direction == "up" else (diff < 0)
        return f"{text}   {'✅' if improved else '⚠️'}"

    if base == 0:
        # 比例口径但基线为 0 —— 算不出百分比。**不能显示成「—」**，
        # 那会把「0 → 2」这种最该看见的恶化藏起来。
        if abs(diff) < 1e-9:
            return "0   ="
        return f"{diff:+.1f}（基线为 0）"

    if style == "pp":
        text = f"{diff:+.1f}pp"
    else:
        text = f"{diff / base * 100:+.1f}%"

    if abs(diff) < 1e-9:
        return f"{text}   ="
    if direction == "neutral":
        return text
    improved = (diff > 0) if direction == "up" else (diff < 0)
    return f"{text}   {'✅' if improved else '⚠️'}"


def render_compare(
    baseline_label: str,
    current_label: str,
    baseline: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> str:
    """并排对比两个实验臂。"""
    lines = ["", f"── 对比：{baseline_label}（基线） vs {current_label}（新） ──"]

    if not baseline or not current:
        if not baseline:
            lines.append(f"⚠️ 基线 {baseline_label!r} 没有任何记录")
        if not current:
            lines.append(f"⚠️ 新臂 {current_label!r} 没有任何记录")
        lines.append("（先各跑一轮，例如：--label A --repeat 3 / --label B --repeat 3）")
        return "\n".join(lines)

    base_m = compare_metrics(baseline)
    cur_m = compare_metrics(current)

    # 老记录没有 eval_passed，会静默退回 ok 口径 —— 那正是把方向显示反的原因。
    # 宁可啰嗦也要提醒，否则「对比表看着有数」会让人误以为结论可信。
    base_missing = len(baseline) - summarize(baseline)["evaluated_count"]
    cur_missing = len(current) - summarize(current)["evaluated_count"]
    if base_missing or cur_missing:
        lines.append(
            f"⚠️ 有记录缺少评测判定字段（基线 {base_missing} 条 / 新臂 {cur_missing} 条），"
            "这些记录只能用 `ok`（计划能否解析）代替判定 —— 口径不同，结论可能反向。"
        )
        lines.append("   重新跑一轮即可消除：uv run task-planner-eval --label ... --repeat 2")
        lines.append("")

    lines.append(
        f"{'指标':<14}{baseline_label:>14}{current_label:>14}{'变化':>16}"
    )
    for name, key, kind, direction, style in _COMPARE_ROWS:
        lines.append(
            f"{name:<14}{_fmt_metric(base_m[key], kind):>14}"
            f"{_fmt_metric(cur_m[key], kind):>14}"
            f"{_fmt_delta(cur_m[key], base_m[key], style, direction):>16}"
        )

    # 样本级差异才是真正值得看的部分 —— 聚合指标会互相抵消
    base_stab = stability(baseline)
    cur_stab = stability(current)
    only_base = sorted(set(base_stab["flaky_ids"]) - set(cur_stab["flaky_ids"]))
    only_cur = sorted(set(cur_stab["flaky_ids"]) - set(base_stab["flaky_ids"]))
    if only_base or only_cur:
        lines.append("")
        lines.append("不稳定样本变化：")
        for sid in only_base:
            lines.append(f"  ✅ {sid}：基线不稳 → 新臂稳定")
        for sid in only_cur:
            lines.append(f"  ⚠️ {sid}：基线稳定 → 新臂不稳")

    lines.append("")
    lines.append(
        "提醒：样本量小时（每臂 < 20 次运行）百分比差异可能只是噪声，别过度解读。"
    )
    return "\n".join(lines)


def render(records: list[dict[str, Any]], *, path: Path, group_by: str | None) -> str:
    if not records:
        return (
            f"📊 运行记录汇总\n{BAR}\n"
            f"文件: {path}\n\n"
            "还没有任何运行记录。\n\n"
            "跑一次规划就会自动写入：\n"
            "  uv run task-planner \"我想周末去成都玩两天\"\n"
            "  uv run task-planner --demo        # 离线也能记录\n"
        )

    summary = summarize(records)
    lines: list[str] = []
    lines.append("📊 运行记录汇总")
    lines.append(BAR)
    lines.append(f"文件: {path}")
    stamps = [r.get("ts", "") for r in records if r.get("ts")]
    if stamps:
        lines.append(f"时间范围: {min(stamps)} ~ {max(stamps)}")
    lines.append("")

    lines.append("── 总体 ──")
    lines.append(
        f"运行次数     {summary['count']}"
        f"   通过 {summary['verdict_count']} ({summary['success_rate']}%)"
    )
    if summary["evaluated_count"]:
        lines.append(
            f"             ↳ 评测判定 {summary['evaluated_count']} 条"
            f"   合规计划产出率 {summary['plan_ok_rate']}%"
            "（与判定率不同：ok 只看计划能否解析）"
        )
    lines.append(f"输出状态     {_distribution(summary['output_status'])}")
    lines.append(f"停止原因     {_distribution(summary['stop_reason'])}")
    if summary["dag_failure_count"]:
        lines.append(f"⚠️ 依赖图不合格 {summary['dag_failure_count']} 次")

    stab = stability(records)
    if stab["samples"]:
        lines.append(
            f"样本稳定性   {stab['samples']} 个样本"
            f"   稳定通过 {stab['stable_pass']}"
            f"   稳定失败 {stab['stable_fail']}"
            f"   ⚠️ 不稳定 {stab['flaky']} ({stab['flaky_rate']}%)"
        )
    if stab["repeated_samples"]:
        lines.append(
            f"计划确定性   {stab['deterministic_samples']}/{stab['repeated_samples']} 个样本"
            f"每次产出同一计划   平均指纹多样性 {stab['avg_digest_variety']}"
        )
    lines.append("")

    lines.append("── 效率 ──")
    lines.append(
        f"平均轮次     {summary['avg_turns']}  (最大 {summary['max_turns']})"
    )
    lines.append(
        f"修复重试     {summary['repair_rate']}% 的运行触发过"
        f"  (平均 {summary['avg_repairs']} 次)"
    )
    lines.append(
        f"工具调用     平均 {summary['avg_tool_calls']} 次"
        f"   失败率 {summary['tool_failure_rate']}%"
    )
    lines.append(f"平均耗时     {_fmt_ms(summary['avg_duration_ms'])}")
    lines.append(
        f"Token        平均 {_fmt_int(summary['avg_tokens'])}"
        f"   总计 {_fmt_int(summary['total_tokens'])}"
    )
    lines.append(f"平均步骤数   {summary['avg_steps']}")
    lines.append("")

    if summary["tools"]:
        lines.append("── 工具使用 ──")
        for name, info in summary["tools"].items():
            lines.append(
                f"{name:<24} {info['count']:>4} 次   "
                f"失败 {info['failed']:>2} ({info['failure_rate']:>5.1f}%)   "
                f"平均 {_fmt_ms(info['avg_latency_ms'])}"
            )
        lines.append("")

    if summary["error_codes"]:
        lines.append("── 失败码分布 ──")
        for code, count in sorted(
            summary["error_codes"].items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"{code:<24} {count}")
        lines.append("")

    if group_by:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            value = _dig(record, group_by)
            groups["(未标注)" if value in (None, "") else str(value)].append(record)

        if len(groups) > 1:
            lines.append(f"── 按 {group_by} 分组 ──")
            lines.append(
                f"{group_by:<20}{'n':>5}{'通过率':>9}{'不稳定':>8}"
                f"{'平均token':>12}{'平均轮次':>10}{'修复率':>9}"
            )
            for name, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                sub = summarize(group)
                stab = stability(group)
                lines.append(
                    f"{name:<20}{sub['count']:>5}{sub['success_rate']:>8.1f}%"
                    f"{stab['flaky']:>8}"
                    f"{_fmt_int(sub['avg_tokens']):>12}{sub['avg_turns']:>10}"
                    f"{sub['repair_rate']:>8.1f}%"
                )
            lines.append("")

            # 同一张表里混了两种口径会误导 —— 老记录没有判定字段，
            # 会静默退回 `ok`（计划能否解析）。必须说明。
            missing = sum(
                1 for r in records if r.get("eval_passed") is None
            )
            if missing:
                lines.append(
                    f"⚠️ 其中 {missing} 条记录缺少评测判定字段（多为修复前写入），"
                    "通过率按 `ok` 口径参与统计 —— 与判定口径不可直接比较。"
                )
                lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task-planner-report",
        description="汇总运行记录，给出通过率 / 效率 / 工具使用等指标。",
    )
    parser.add_argument("--path", type=Path, default=None, help="运行记录文件路径")
    parser.add_argument("--last", type=int, default=0, help="只看最近 N 次运行")
    parser.add_argument("--label", default="", help="只统计该 label 的记录")
    parser.add_argument(
        "--group-by",
        default="",
        help=(
            "按该字段分组对比。支持点号路径，例如 label / source / model / "
            "params.temperature / params.max_turns"
        ),
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("基线", "新臂"),
        default=None,
        help=(
            "并排对比两个 label 的指标与变化量，例如 "
            "--compare exp-t02 exp-t00。这是做 A/B 实验的主入口。"
        ),
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（便于画图）")
    parser.add_argument(
        "--include-rotated", action="store_true", help="连同轮转的历史文件一起统计"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.path or log_path()
    records = load_runs(args.path, include_rotated=args.include_rotated)

    # --compare 自己按 label 过滤，不走 --label（否则会把两臂都过滤掉）
    if args.compare:
        baseline_label, current_label = args.compare
        all_records = records
        baseline = [r for r in all_records if r.get("label") == baseline_label]
        current = [r for r in all_records if r.get("label") == current_label]

        if args.json:
            print(
                json.dumps(
                    {
                        "baseline": {
                            "label": baseline_label,
                            "metrics": compare_metrics(baseline),
                            "stability": stability(baseline),
                        },
                        "current": {
                            "label": current_label,
                            "metrics": compare_metrics(current),
                            "stability": stability(current),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
            )
            return 0

        print(render_compare(baseline_label, current_label, baseline, current))
        return 0

    if args.label:
        records = [r for r in records if r.get("label") == args.label]
    if args.last > 0:
        records = records[-args.last :]

    if args.json:
        payload = {
            "path": str(path),
            "group_by": args.group_by or None,
            "summary": summarize(records),
            "stability": stability(records),
        }
        if args.group_by:
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in records:
                value = _dig(record, args.group_by)
                groups["(未标注)" if value in (None, "") else str(value)].append(record)
            payload["groups"] = {k: summarize(v) for k, v in groups.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(render(records, path=path, group_by=args.group_by or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
