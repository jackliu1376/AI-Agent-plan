"""运行记录持久化 —— 把每次规划的结果落成一行 JSONL。

为什么需要它
------------
没有运行记录时，改提示词 / 调参数只能靠单次人肉观察：
**分不清「模型变好了」和「这次运气好」**。有了它才能回答：

- 成功率是多少？改完之后涨了还是跌了？
- 平均轮次 / 修复次数 / token 用量是多少？
- 哪个工具失败最多？失败码分布如何？
- 加了「数据时效」字段之后，修复重试率有没有上升？

配合 ``agent/run_report.py`` 做汇总。

设计约束
--------
1. **绝不抛异常。** 记录失败不能影响正在跑的规划 —— 日志是旁路，不是主路。
2. **追加写 JSONL。** 一行一条，坏行不影响其他行，用 ``jq`` 或 pandas 都能读。
3. **按大小轮转。** 只保留一代历史文件，避免无限增长。
4. **默认开启，测试关闭。** 通过 ``RUN_LOG_ENABLED`` 控制；
   测试里由 conftest 的 autouse fixture 关掉，避免污染仓库。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.loop import PlanResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "runs.jsonl"

# 单个记录里 tool_args 的最大字符数（save_itinerary 的 content 可能很大）
MAX_ARGS_CHARS = 400

# 超过该大小就轮转出 .1 历史文件
MAX_LOG_BYTES = 50 * 1024 * 1024


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def log_path() -> Path:
    """运行记录文件路径（可由 ``RUN_LOG_PATH`` 覆盖）。"""
    raw = _env("RUN_LOG_PATH")
    if not raw:
        return DEFAULT_LOG_PATH
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


def is_enabled() -> bool:
    """是否启用运行记录。``RUN_LOG_ENABLED=0`` 可关闭。"""
    return _env("RUN_LOG_ENABLED", "1") not in {"0", "false", "no"}


def _trim_args(args: Any) -> Any:
    """把工具参数归一化成 **JSON 安全**的结构，并截断过大的内容。

    归一化这一步很重要：``build_record`` 的产出应当可以独立序列化，
    否则调用方必须记得传 ``default=str`` —— 这种隐性契约迟早会被踩。
    """
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
        normalized = json.loads(text)
    except Exception:  # noqa: BLE001 - 连 str() 都可能抛（自定义 __repr__）
        return {"_unserializable": True}

    if len(text) <= MAX_ARGS_CHARS:
        return normalized
    return {"_truncated": text[:MAX_ARGS_CHARS] + f"…(+{len(text) - MAX_ARGS_CHARS} 字符)"}


def settings_params(settings: Any) -> dict[str, Any]:
    """从 Settings 里挑出**会影响模型行为**的字段，作为实验条件快照。

    只记这几个而不是整个 Settings：其余字段（api_key / prompt_path）
    要么是机密，要么与本次实验无关，写进日志只会让 diff 变吵。
    """
    keys = ("temperature", "max_turns", "tool_budget", "llm_timeout", "llm_max_retries")
    return {key: getattr(settings, key) for key in keys if hasattr(settings, key)}


def _plan_digest(result: PlanResult) -> str:
    """计划内容的短哈希，用于判断两次运行是否产出了同一个计划。"""
    if result.plan is None:
        return ""
    try:
        blob = json.dumps(result.plan.model_dump(), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def build_record(
    result: PlanResult,
    task: str,
    *,
    source: str = "unknown",
    label: str = "",
    model: str = "",
    sample_id: str = "",
    attempt: int = 1,
    params: dict[str, Any] | None = None,
    eval_passed: bool | None = None,
    eval_failures: list[str] | None = None,
) -> dict[str, Any]:
    """把一次运行压成一行便于统计的记录。

    刻意**不写入完整 messages**（体积大且含系统提示词），也不写 markdown 正文 ——
    需要排查细节时用 ``--save-plan`` 单独落盘。

    ``params`` 是**实验条件快照**（temperature / max_turns / ...）。

    为什么不能只靠 ``label``：label 是人手写的字符串，写错了不会有任何报错。
    当你要对比「temperature=0 是不是更好」时，如果记录里只有
    ``label="exp-t00"``，你无法验证那一批**真的**用了 0 —— 可能是环境变量没设上、
    也可能是手滑打错。把实际生效的参数写进记录，结论才立得住。

    ``eval_passed`` / ``eval_failures`` 是**评测判定**，与 ``ok`` **不是一回事**：

    - ``ok``：是否产出了合规计划（``PlanResult.ok``）
    - ``eval_passed``：是否满足样本声明的**期望行为**（``check()`` 的结论）

    两者会反向。实测踩坑：样本 ``injection-01`` 的期望里没有 ``ok`` 字段，
    于是「模型拒绝且没产出计划」算通过；而「产出了合规计划但正文提到了
    禁用词」算失败。只记 ``ok`` 的话，对比报告会把方向显示反 ——
    当时 ``--compare`` 说 temperature=0 更差，实际是更好。
    """
    trace = result.trace
    return {
        "run_id": uuid.uuid4().hex[:12],
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": label or _env("RUN_LABEL"),
        "source": source,
        "sample_id": sample_id,
        # 同一 sample 重复跑时，用它区分第几次（LLM 有随机性，单次结果不可信）
        "attempt": attempt,
        "model": model,
        "params": dict(params or {}),
        "task": task,
        # -- 评测判定（仅 source=eval 时有值）--
        "eval_passed": eval_passed,
        "eval_failures": list(eval_failures or []),
        # -- 结果 --
        "ok": result.ok,
        "output_status": trace.output_status,
        "stop_reason": trace.stop_reason,
        "error": trace.error,
        # -- 过程 --
        "turns": trace.turns,
        "repairs": trace.repairs,
        "interrupts": trace.interrupts,
        "budget": trace.budget,
        "budget_used": trace.budget_used,
        "budget_exceeded": trace.budget_exceeded,
        "injection_flags": len(trace.injection_flags),
        "duration_ms": trace.duration_ms,
        # -- 产出结构 --
        "step_count": result.step_count,
        "phase_count": len(result.plan.phases) if result.plan else 0,
        "dag_ok": trace.dag_ok,
        "dag_error": trace.dag_error,
        "plan_digest": _plan_digest(result),
        "has_data_freshness": bool(result.plan and result.plan.data_freshness),
        "verification_channels": len(result.plan.verification_channels) if result.plan else 0,
        # -- 工具与用量 --
        "tools_used": trace.tools_used(),
        "tool_call_count": len(trace.tool_calls),
        "tool_failure_count": len(trace.failed_calls),
        "tool_calls": [
            {
                "tool": c.tool,
                "ok": c.ok,
                "cached": c.cached,
                "error_code": c.error_code,
                "latency_ms": c.latency_ms,
                "attempts": c.attempts,
                "args": _trim_args(c.args),
            }
            for c in trace.tool_calls
        ],
        "usage": trace.usage,
    }


def append_record(entry: dict[str, Any], path: Path | None = None) -> bool:
    """把一条**已经构造好**的记录追加到日志。失败返回 False，不抛异常。

    与 ``record_run`` 的分工：后者是「构造 + 写」的一体化入口，适合单次调用；
    交错实验里同一条记录既要参与内存中的 A/B 对比、又可能要落盘，
    构造一次再决定是否写更自然（也避免重复计算 plan_digest）。
    """
    try:
        target = path or log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)

        line = json.dumps(entry, ensure_ascii=False, default=str)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - 日志是旁路，绝不能影响主流程
        return False


def record_run(
    result: PlanResult,
    task: str,
    *,
    source: str = "unknown",
    label: str = "",
    model: str = "",
    sample_id: str = "",
    attempt: int = 1,
    params: dict[str, Any] | None = None,
    eval_passed: bool | None = None,
    eval_failures: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    """追加一条运行记录。**任何失败都只返回 None，不抛异常。**

    返回写入的记录（便于测试断言），被禁用或写失败时返回 ``None``。
    """
    if not is_enabled() and path is None:
        return None

    entry = build_record(
        result,
        task,
        source=source,
        label=label,
        model=model,
        sample_id=sample_id,
        attempt=attempt,
        params=params,
        eval_passed=eval_passed,
        eval_failures=eval_failures,
    )
    return entry if append_record(entry, path) else None


def _rotate_if_needed(path: Path) -> None:
    """超过大小上限时，把当前文件改名为 .1（覆盖旧的历史文件）。"""
    try:
        if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass  # 轮转失败不该阻断写入


def load_runs(path: Path | None = None, *, include_rotated: bool = False) -> list[dict[str, Any]]:
    """读回运行记录。坏行会被跳过而不是让整个文件读不出来。"""
    target = path or log_path()
    files = [target]
    if include_rotated:
        rotated = target.with_suffix(target.suffix + ".1")
        if rotated.exists():
            files.insert(0, rotated)

    records: list[dict[str, Any]] = []
    for file in files:
        if not file.exists():
            continue
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # 容忍被截断的坏行
            if isinstance(obj, dict):
                records.append(obj)
    return records


__all__ = [
    "DEFAULT_LOG_PATH",
    "append_record",
    "build_record",
    "is_enabled",
    "load_runs",
    "log_path",
    "record_run",
    "settings_params",
]
