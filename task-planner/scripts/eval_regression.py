"""评测回归检查：把「这次跑的」和「上次认可的」逐样本对比。

为什么需要它
------------
``task-planner-report --compare`` 比的是**聚合指标**（成功率、平均轮次）。
聚合有个致命问题：**两条样本一升一降，总数不变，看起来「没变化」**。

改提示词最容易踩的就是这个 —— 修好了 A，同时弄坏了 B，报告显示 87.5% → 87.5%，
你以为没事，其实行为已经变了。

这个脚本比的是**逐样本的通过状态**，能明确告诉你：
「injection-02 从通过变成失败了」。

用法
----
::

    # 1. 跑一轮评测（会写进 logs/runs.jsonl）
    uv run task-planner-eval --tier smoke --label after-prompt-v3

    # 2. 和基线对比
    uv run python scripts/eval_regression.py check \\
        --baseline evals/baseline.json --label after-prompt-v3

    # 3. 认可这次结果，更新基线
    uv run python scripts/eval_regression.py write \\
        --baseline evals/baseline.json --label after-prompt-v3

退出码：检测到回归返回 1，否则 0 —— 可以直接串进 shell 的 ``&&``。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.run_log import log_path  # noqa: E402

DEFAULT_BASELINE = PROJECT_ROOT / "evals" / "baseline.json"

# 基线的 schema 版本。字段变了就加一，读旧基线时能给出明确提示而不是瞎猜。
BASELINE_VERSION = 1


# ---------------------------------------------------------------------------
# 读运行记录
# ---------------------------------------------------------------------------


def load_eval_records(log_file: Path, label: str = "") -> list[dict[str, Any]]:
    """从 JSONL 里读出评测记录。

    ``label`` 为空时取**最近一个** label 的全部记录 —— 按 ``ts`` 找最新的那批。
    这样不用每次手抄 label，直接 ``--label`` 省略即可。
    """
    if not log_file.exists():
        raise SystemExit(f"运行记录不存在：{log_file}\n先跑一轮评测：uv run task-planner-eval")

    records: list[dict[str, Any]] = []
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # 坏行跳过：JSONL 的约定就是一行坏了不影响其他行
            continue
        if record.get("source") != "eval":
            continue
        if label and record.get("label") != label:
            continue
        records.append(record)

    if not records:
        suffix = f"（label={label}）" if label else ""
        raise SystemExit(f"没有找到评测记录{suffix}。先跑一轮：uv run task-planner-eval")

    if not label:
        # 没指定 label 时只保留最新那一批，避免把历史所有运行混在一起
        latest = max(str(r.get("ts") or "") for r in records)
        records = [r for r in records if str(r.get("ts") or "") == latest]

    return records


def aggregate(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 ``sample_id`` 聚合。

    同一条样本可能因为 ``--repeat`` 跑多次。判据取**全部通过才算通过** ——
    单次通过不代表稳定通过，而回归检查关心的是「稳定行为有没有变坏」。
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        sample_id = str(record.get("sample_id") or "")
        if sample_id:
            grouped[sample_id].append(record)

    summary: dict[str, dict[str, Any]] = {}
    for sample_id, items in grouped.items():
        items.sort(key=lambda r: int(r.get("attempt") or 1))
        judged = [r for r in items if r.get("eval_passed") is not None]
        # 有判定的取「全部通过」，一条都没判定的记 None（不参与回归对比）
        passed: bool | None = (
            all(bool(r["eval_passed"]) for r in judged) if judged else None
        )

        last = items[-1]
        failures: list[str] = []
        for item in judged:
            for failure in item.get("eval_failures") or []:
                if failure not in failures:
                    failures.append(failure)

        summary[sample_id] = {
            "passed": passed,
            "failures": failures,
            "attempts": len(items),
            "ok": bool(last.get("ok")),
            "turns": int(last.get("turns") or 0),
            "duration_ms": int(last.get("duration_ms") or 0),
            "step_count": int(last.get("step_count") or 0),
        }
    return summary


# ---------------------------------------------------------------------------
# 基线读写
# ---------------------------------------------------------------------------


def write_baseline(path: Path, label: str, summary: dict[str, dict[str, Any]]) -> None:
    payload = {
        "version": BASELINE_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label,
        "samples": summary,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_baseline(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not path.exists():
        raise SystemExit(
            f"基线文件不存在：{path}\n先跑一轮评测再用 write 生成：\n"
            f"  uv run python scripts/eval_regression.py write --label <本轮 label>"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != BASELINE_VERSION:
        raise SystemExit(
            f"基线格式版本不匹配（文件 {version}，脚本 {BASELINE_VERSION}）。"
            "重新生成一次基线即可。"
        )
    return payload, payload.get("samples") or {}


# ---------------------------------------------------------------------------
# 对比
# ---------------------------------------------------------------------------


def compare(
    baseline: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]
) -> dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]]:
    """返回 ``{"regressed": [...], "fixed": [...], "added": [...], "removed": [...]}``。"""
    regressed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    fixed: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    added: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    for sample_id, now in current.items():
        before = baseline.get(sample_id)
        if before is None:
            added.append((sample_id, {}, now))
            continue
        was, is_now = before.get("passed"), now.get("passed")
        if was is None or is_now is None:
            continue  # 未判定的样本不参与回归判定
        if was and not is_now:
            regressed.append((sample_id, before, now))
        elif not was and is_now:
            fixed.append((sample_id, before, now))

    removed: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        (sid, item, {}) for sid, item in baseline.items() if sid not in current
    ]
    return {"regressed": regressed, "fixed": fixed, "added": added, "removed": removed}


def _fmt_metric(before: Any, now: Any, unit: str = "") -> str:
    if not isinstance(before, int | float) or not isinstance(now, int | float):
        return ""
    delta = now - before
    if delta == 0:
        return ""
    arrow = "↑" if delta > 0 else "↓"
    return f"（{arrow}{abs(delta):g}{unit}）"


def render(
    baseline_label: str,
    label: str,
    result: dict[str, list[tuple[str, Any, Any]]],
    unchanged: list[str],
) -> bool:
    """打印报告，返回「是否检测到回归」。"""
    regressed = result["regressed"]
    fixed = result["fixed"]
    added = result["added"]
    removed = result["removed"]

    print(f"\n逐样本回归检查：{baseline_label} → {label or '(最近一次)'}")
    print("=" * 62)

    if regressed:
        print(f"\n❌ 回归 {len(regressed)} 条（原来通过，现在失败）")
        for sample_id, before, now in regressed:
            print(f"   {sample_id}")
            for failure in now.get("failures") or []:
                print(f"      · {failure}")
            turns = _fmt_metric(before.get("turns"), now.get("turns"), " 轮")
            if turns:
                print(f"      轮次 {before.get('turns')} → {now.get('turns')} {turns}")

    if fixed:
        print(f"\n✅ 改善 {len(fixed)} 条（原来失败，现在通过）")
        for sample_id, _before, _now in fixed:
            print(f"   {sample_id}")

    if added:
        print(f"\n➕ 新增 {len(added)} 条（基线里没有，不计入回归）")
        for sample_id, _before, now in added:
            state = "通过" if now.get("passed") else "失败"
            print(f"   {sample_id}  {state}")

    if removed:
        print(f"\n➖ 消失 {len(removed)} 条（基线里有，这次没跑）")
        for sample_id, _before, _now in removed:
            print(f"   {sample_id}  —— 样本被删/改名了，或这次没跑到")

    total = len(regressed) + len(fixed) + len(added) + len(removed)
    if total == 0:
        print(f"\n✅ 全部 {len(unchanged)} 条样本状态与基线一致")
    elif not regressed:
        print(f"\n✅ 没有回归（{len(unchanged)} 条持平）")

    return bool(regressed)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_regression",
        description="评测回归检查：逐样本对比基线与本轮结果。",
    )
    parser.add_argument(
        "action",
        choices=["check", "write"],
        help="check=对比，write=用本轮结果覆盖基线",
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="基线文件路径")
    parser.add_argument("--label", default="", help="用哪个 label 的运行记录（省略=最近一次）")
    parser.add_argument("--log", type=Path, default=None, help="运行记录路径")
    args = parser.parse_args(argv)

    log_file = args.log or log_path()
    records = load_eval_records(log_file, args.label)
    summary = aggregate(records)
    effective_label = args.label or str(records[-1].get("label") or "")

    judged = sum(1 for item in summary.values() if item["passed"] is not None)
    passed = sum(1 for item in summary.values() if item["passed"] is True)
    print(f"本轮：{len(summary)} 条样本，{judged} 条有判定，通过 {passed} 条")

    if args.action == "write":
        write_baseline(args.baseline, effective_label, summary)
        print(f"✅ 基线已写入 {args.baseline}")
        return 0

    baseline_payload, baseline_samples = read_baseline(args.baseline)
    result = compare(baseline_samples, summary)

    # 算一下「完全没变」的数量，报告里用来交代覆盖面
    unchanged = [
        sid
        for sid, now in summary.items()
        if sid in baseline_samples
        and now["passed"] is not None
        and now["passed"] == baseline_samples[sid].get("passed")
    ]

    regressed = render(
        str(baseline_payload.get("label") or "(未命名基线)"),
        effective_label,
        result,
        unchanged,
    )
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
