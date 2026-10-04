"""运行记录与汇总报告的测试。

覆盖重点：
1. **绝不抛异常** —— 记录失败不能影响正在跑的规划
2. **默认不在测试里写** —— 否则会污染仓库的 logs/
3. **坏行容错** —— JSONL 被截断时不能整个文件读不出来
4. **统计口径正确** —— 报告数字算错比没有报告更糟
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.demo import ScriptedLLM
from agent.loop import Orchestrator
from agent.run_log import (
    build_record,
    is_enabled,
    load_runs,
    log_path,
    record_run,
)
from agent.run_report import main as report_main
from agent.run_report import render, summarize
from tests.conftest import make_plan, plan_turn, tool_turn

WEATHER = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}


@pytest.fixture
def sample_result(settings, runner):
    """跑一次真实的编排循环，拿到带 trace 的 PlanResult。"""
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER)),
            plan_turn(make_plan()),
        ]
    )
    return Orchestrator(llm, runner, settings).run("成都 2 日游")


# ---------------------------------------------------------------------------
# 记录写入与读取
# ---------------------------------------------------------------------------


def test_record_run_writes_one_jsonl_line(sample_result, run_log_path: Path) -> None:
    entry = record_run(sample_result, "成都 2 日游", source="test", path=run_log_path)

    assert entry is not None
    lines = run_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    parsed = json.loads(lines[0])
    assert parsed["run_id"] == entry["run_id"]
    assert parsed["task"] == "成都 2 日游"
    assert parsed["source"] == "test"


def test_record_contains_evaluation_fields(sample_result, run_log_path: Path) -> None:
    """评测需要的字段一个都不能少。"""
    entry = record_run(sample_result, "任务", path=run_log_path)
    assert entry is not None

    for field in (
        "run_id",
        "ts",
        "label",
        "source",
        "model",
        "task",
        "ok",
        "output_status",
        "stop_reason",
        "turns",
        "repairs",
        "interrupts",
        "budget",
        "budget_used",
        "duration_ms",
        "step_count",
        "phase_count",
        "dag_ok",
        "plan_digest",
        "tool_call_count",
        "tool_failure_count",
        "tool_calls",
        "usage",
    ):
        assert field in entry, f"缺少字段 {field}"

    assert entry["ok"] is True
    assert entry["output_status"] == "ok"
    assert entry["duration_ms"] >= 0
    assert entry["plan_digest"]


def test_appends_rather_than_overwrites(sample_result, run_log_path: Path) -> None:
    record_run(sample_result, "任务一", path=run_log_path)
    record_run(sample_result, "任务二", path=run_log_path)

    records = load_runs(run_log_path)
    assert len(records) == 2
    assert [r["task"] for r in records] == ["任务一", "任务二"]


def test_load_runs_skips_corrupt_lines(sample_result, run_log_path: Path) -> None:
    """JSONL 被截断时，坏行跳过而不是整个文件读不出来。"""
    record_run(sample_result, "好的记录", path=run_log_path)
    with run_log_path.open("a", encoding="utf-8") as fh:
        fh.write("{这行坏了\n")
        fh.write("\n")
    record_run(sample_result, "另一条好记录", path=run_log_path)

    records = load_runs(run_log_path)
    assert [r["task"] for r in records] == ["好的记录", "另一条好记录"]


def test_load_runs_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_runs(tmp_path / "不存在.jsonl") == []


# ---------------------------------------------------------------------------
# 安全性：绝不能影响主流程
# ---------------------------------------------------------------------------


def test_disabled_by_default_in_tests() -> None:
    """conftest 的 autouse fixture 必须关掉记录，避免污染仓库。"""
    assert is_enabled() is False


def test_record_returns_none_when_disabled(sample_result) -> None:
    assert record_run(sample_result, "任务") is None


def test_explicit_path_bypasses_disable(sample_result, run_log_path: Path) -> None:
    """显式传 path 时照常写入 —— 方便测试与脚本化采集。"""
    assert record_run(sample_result, "任务", path=run_log_path) is not None
    assert run_log_path.exists()


def test_never_raises_on_unwritable_path(sample_result, tmp_path: Path) -> None:
    """写不进去也必须安静返回 None，不能把正在跑的规划带崩。"""
    bogus = tmp_path / "a.txt" / "b.jsonl"  # 父路径是文件，必然失败
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    assert record_run(sample_result, "任务", path=bogus) is None


def test_log_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.jsonl"
    monkeypatch.setenv("RUN_LOG_PATH", str(target))
    assert log_path() == target


# ---------------------------------------------------------------------------
# 体积控制与轮转
# ---------------------------------------------------------------------------


def test_large_tool_args_are_truncated(sample_result) -> None:
    """save_itinerary 的 content 可能很大，单条记录不能撑爆日志。"""
    from agent.run_log import MAX_ARGS_CHARS, _trim_args

    small = {"city": "成都"}
    assert _trim_args(small) == small

    huge = {"content": "x" * (MAX_ARGS_CHARS * 3)}
    trimmed = _trim_args(huge)
    assert "_truncated" in trimmed
    assert len(trimmed["_truncated"]) < len(huge["content"])


def test_unserializable_args_are_handled(sample_result) -> None:
    """工具参数里有不可序列化对象时，记录本身仍要能被独立序列化。"""
    from agent.run_log import build_record

    sample_result.trace.tool_calls[0].args = {"obj": object(), "nested": {1, 2}}
    entry = build_record(sample_result, "任务", source="test")

    # 不传 default=str —— build_record 的产出必须自己就是 JSON 安全的
    json.dumps(entry, ensure_ascii=False)


def test_unserializable_args_fall_back_when_dump_fails(sample_result) -> None:
    """连 str() 都救不了时，退化成标记而不是抛异常。"""
    from agent.run_log import _trim_args

    class _Exploding:
        def __repr__(self) -> str:
            raise RuntimeError("连 repr 都炸")

    assert _trim_args({"bad": _Exploding()}) == {"_unserializable": True}


def test_rotation_when_file_too_large(
    sample_result, run_log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过上限时轮转出 .1，避免日志无限增长。"""
    import agent.run_log as run_log

    monkeypatch.setattr(run_log, "MAX_LOG_BYTES", 10)
    record_run(sample_result, "第一条", path=run_log_path)
    record_run(sample_result, "第二条", path=run_log_path)

    rotated = run_log_path.with_suffix(run_log_path.suffix + ".1")
    assert rotated.exists(), "应轮转出历史文件"
    assert [r["task"] for r in load_runs(run_log_path)] == ["第二条"]
    assert [r["task"] for r in load_runs(run_log_path, include_rotated=True)] == [
        "第一条",
        "第二条",
    ]


# ---------------------------------------------------------------------------
# label：A/B 对比的基础
# ---------------------------------------------------------------------------


def test_label_is_recorded(sample_result, run_log_path: Path) -> None:
    entry = record_run(sample_result, "任务", label="prompt-v2", path=run_log_path)
    assert entry is not None
    assert entry["label"] == "prompt-v2"


def test_label_falls_back_to_env(
    sample_result, run_log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_LABEL", "from-env")
    entry = record_run(sample_result, "任务", path=run_log_path)
    assert entry is not None
    assert entry["label"] == "from-env"


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------


def _fake_record(**overrides) -> dict:
    base = {
        "run_id": "x",
        "ts": "2026-10-03T10:00:00+08:00",
        "label": "",
        "source": "cli",
        "ok": True,
        "output_status": "ok",
        "stop_reason": "model_finished",
        "turns": 2,
        "repairs": 0,
        "duration_ms": 1000,
        "step_count": 10,
        "dag_ok": True,
        "tool_call_count": 1,
        "usage": {"total_tokens": 1000},
        "tool_calls": [{"tool": "t1", "ok": True, "latency_ms": 100}],
    }
    base.update(overrides)
    return base


def test_summarize_empty() -> None:
    assert summarize([]) == {"count": 0}


def test_summarize_success_rate_and_averages() -> None:
    records = [
        _fake_record(ok=True, turns=2, usage={"total_tokens": 1000}),
        _fake_record(ok=True, turns=4, usage={"total_tokens": 3000}),
        _fake_record(ok=False, output_status="invalid_output", turns=6, repairs=1,
                     usage={"total_tokens": 2000}),
    ]
    s = summarize(records)

    assert s["count"] == 3
    assert s["ok_count"] == 2
    assert s["success_rate"] == pytest.approx(66.7)
    assert s["avg_turns"] == 4.0
    assert s["avg_tokens"] == 2000.0
    assert s["total_tokens"] == 6000
    assert s["repair_rate"] == pytest.approx(33.3)
    assert s["output_status"]["invalid_output"] == 1


def test_summarize_tool_failure_rate() -> None:
    records = [
        _fake_record(
            tool_calls=[
                {"tool": "a", "ok": True, "latency_ms": 10},
                {"tool": "a", "ok": False, "error_code": "UPSTREAM_TIMEOUT", "latency_ms": 20},
                {"tool": "b", "ok": True, "latency_ms": 30},
            ]
        )
    ]
    s = summarize(records)

    assert s["tool_failure_rate"] == pytest.approx(33.3)
    assert s["error_codes"] == {"UPSTREAM_TIMEOUT": 1}
    assert s["tools"]["a"]["count"] == 2
    assert s["tools"]["a"]["failed"] == 1
    assert s["tools"]["a"]["failure_rate"] == pytest.approx(50.0)


def test_summarize_counts_dag_failures() -> None:
    s = summarize([_fake_record(dag_ok=False), _fake_record(dag_ok=True)])
    assert s["dag_failure_count"] == 1


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def test_render_empty_is_helpful(tmp_path: Path) -> None:
    text = render([], path=tmp_path / "runs.jsonl", group_by=None)
    assert "还没有任何运行记录" in text
    assert "uv run task-planner" in text, "空状态要告诉用户怎么产生记录"


def test_render_contains_key_sections() -> None:
    text = render([_fake_record(), _fake_record(ok=False)], path=Path("runs.jsonl"), group_by=None)

    for section in ("运行记录汇总", "总体", "效率", "工具使用"):
        assert section in text
    assert "通过" in text


def test_render_groups_by_label() -> None:
    records = [
        _fake_record(label="baseline", usage={"total_tokens": 1000}),
        _fake_record(label="baseline", usage={"total_tokens": 1000}),
        _fake_record(label="prompt-v2", usage={"total_tokens": 800}),
    ]
    text = render(records, path=Path("runs.jsonl"), group_by="label")

    assert "按 label 分组" in text
    assert "baseline" in text
    assert "prompt-v2" in text


def test_render_skips_grouping_with_single_group() -> None:
    text = render([_fake_record(label="only")], path=Path("runs.jsonl"), group_by="label")
    assert "按 label 分组" not in text


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def test_report_main_json_output(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_run(sample_result, "任务", path=run_log_path)

    code = report_main(["--path", str(run_log_path), "--json"])
    assert code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["count"] == 1
    assert payload["summary"]["ok_count"] == 1


def test_report_main_last_filter(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for i in range(3):
        record_run(sample_result, f"任务{i}", path=run_log_path)

    report_main(["--path", str(run_log_path), "--last", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["count"] == 1


def test_report_main_label_filter(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_run(sample_result, "a", label="x", path=run_log_path)
    record_run(sample_result, "b", label="y", path=run_log_path)

    report_main(["--path", str(run_log_path), "--label", "x", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["count"] == 1


def test_build_record_is_json_serializable(sample_result) -> None:
    entry = build_record(sample_result, "任务", source="test")
    json.dumps(entry, ensure_ascii=False)  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 实验条件快照（params）
# ---------------------------------------------------------------------------


def test_settings_params_captures_experiment_condition(settings) -> None:
    """实验条件必须能从 Settings 里提取出来，否则 A/B 对比无从验证。"""
    from agent.run_log import settings_params

    params = settings_params(settings)

    assert "temperature" in params
    assert "max_turns" in params
    assert "tool_budget" in params


def test_settings_params_omits_secrets(settings) -> None:
    """api_key / prompt_path 不该进日志 —— 一个是机密，一个与实验无关。"""
    from agent.run_log import settings_params

    params = settings_params(settings)

    assert "api_key" not in params
    assert "prompt_path" not in params
    assert "base_url" not in params


def test_build_record_includes_params(sample_result) -> None:
    """params 是「这次跑的是什么配置」的自证，不能只靠 label。"""
    entry = build_record(
        sample_result,
        "任务",
        label="exp-t00",
        params={"temperature": 0.0, "max_turns": 8},
    )

    assert entry["params"] == {"temperature": 0.0, "max_turns": 8}


def test_build_record_defaults_params_to_empty_dict(sample_result) -> None:
    """没传 params 时给空字典，而不是缺字段 —— 让下游不必判 None。"""
    entry = build_record(sample_result, "任务")
    assert entry["params"] == {}


# ---------------------------------------------------------------------------
# 点号路径与稳定性统计
# ---------------------------------------------------------------------------


def test_dig_reads_nested_path() -> None:
    from agent.run_report import _dig

    record = {"params": {"temperature": 0.0}, "label": "a"}

    assert _dig(record, "label") == "a"
    assert _dig(record, "params.temperature") == 0.0
    assert _dig(record, "params.missing") is None
    assert _dig(record, "nope.deep.path") is None


def test_stability_separates_flaky_from_failed() -> None:
    from agent.run_report import stability

    records = [
        {"sample_id": "s1", "ok": True},
        {"sample_id": "s1", "ok": True},
        {"sample_id": "s2", "ok": True},
        {"sample_id": "s2", "ok": False},   # 时过时不过
        {"sample_id": "s3", "ok": False},
        {"sample_id": "s3", "ok": False},   # 稳定失败
    ]
    st = stability(records)

    assert st["samples"] == 3
    assert st["stable_pass"] == 1
    assert st["stable_fail"] == 1
    assert st["flaky"] == 1
    assert st["flaky_ids"] == ["s2"]
    assert st["flaky_rate"] == pytest.approx(33.3)


def test_stability_ignores_records_without_sample_id() -> None:
    """非评测来源（CLI/Web）没有 sample_id，不该被算成一个「样本」。"""
    from agent.run_report import stability

    st = stability([{"ok": True}, {"ok": False}, {"sample_id": "", "ok": True}])
    assert st["samples"] == 0


# ---------------------------------------------------------------------------
# A/B 对比
# ---------------------------------------------------------------------------


def test_render_compare_shows_both_arms_and_delta() -> None:
    from agent.run_report import render_compare

    baseline = [_fake_record(ok=True), _fake_record(ok=False)]
    current = [_fake_record(ok=True), _fake_record(ok=True)]

    text = render_compare("A", "B", baseline, current)

    assert "对比" in text
    assert "通过率" in text
    assert "不稳定样本" in text
    assert "+50.0pp" in text, "两臂判定通过率 50% → 100%，差值是 +50 个百分点"


def test_render_compare_warns_when_arm_missing() -> None:
    from agent.run_report import render_compare

    text = render_compare("A", "B", [], [_fake_record(ok=True)])

    assert "没有任何记录" in text
    assert "--label" in text, "要告诉用户怎么补数据"


def test_compare_metrics_reports_flaky_count() -> None:
    from agent.run_report import compare_metrics

    records = [
        {"sample_id": "s1", "ok": True, "turns": 2, "usage": {"total_tokens": 100},
         "duration_ms": 1000, "step_count": 5, "dag_ok": True, "tool_calls": []},
        {"sample_id": "s1", "ok": False, "turns": 3, "usage": {"total_tokens": 200},
         "duration_ms": 1000, "step_count": 5, "dag_ok": True, "tool_calls": []},
    ]
    metrics = compare_metrics(records)

    assert metrics["flaky"] == 1
    assert metrics["runs"] == 2


def test_report_main_compare_end_to_end(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--compare 是 A/B 实验的主入口，必须能跑通。"""
    record_run(sample_result, "a", label="exp-t02", path=run_log_path)
    record_run(sample_result, "b", label="exp-t00", path=run_log_path)

    code = report_main(["--path", str(run_log_path), "--compare", "exp-t02", "exp-t00"])
    out = capsys.readouterr().out

    assert code == 0
    assert "exp-t02" in out and "exp-t00" in out


def test_report_main_compare_json(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record_run(sample_result, "a", label="A", path=run_log_path)
    record_run(sample_result, "b", label="B", path=run_log_path)

    report_main(["--path", str(run_log_path), "--compare", "A", "B", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["baseline"]["label"] == "A"
    assert payload["current"]["label"] == "B"
    assert "success_rate" in payload["baseline"]["metrics"]


def test_group_by_dotted_path_splits_by_temperature(
    sample_result, run_log_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """按实际参数分组（而不是按 label）—— 这是验证「label 有没有说谎」的手段。"""
    record_run(sample_result, "a", label="x", path=run_log_path,
               params={"temperature": 0.2})
    record_run(sample_result, "b", label="y", path=run_log_path,
               params={"temperature": 0.0})

    report_main(["--path", str(run_log_path), "--group-by", "params.temperature", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert set(payload["groups"]) == {"0.2", "0.0"}


def test_render_groups_by_dotted_path() -> None:
    from agent.run_report import render

    records = [
        _fake_record(params={"temperature": 0.2}),
        _fake_record(params={"temperature": 0.0}),
    ]
    text = render(records, path=Path("x.jsonl"), group_by="params.temperature")

    assert "params.temperature" in text
    assert "0.2" in text and "0.0" in text


# ---------------------------------------------------------------------------
# 实验条件快照（params）
# ---------------------------------------------------------------------------


def test_stability_measures_digest_variety() -> None:
    """同一样本重复跑，计划指纹越少越确定 —— 这是温度实验的核心指标。"""
    from agent.run_report import stability

    records = [
        {"sample_id": "s1", "ok": True, "plan_digest": "aaa"},
        {"sample_id": "s1", "ok": True, "plan_digest": "aaa"},   # 完全一致
        {"sample_id": "s2", "ok": True, "plan_digest": "bbb"},
        {"sample_id": "s2", "ok": True, "plan_digest": "ccc"},   # 每次都不一样
    ]
    st = stability(records)

    assert st["repeated_samples"] == 2
    assert st["deterministic_samples"] == 1, "只有 s1 每次产出同一计划"
    assert st["avg_digest_variety"] == pytest.approx(1.5)


def test_stability_ignores_single_run_for_variety() -> None:
    """只跑过一次的样本没有「多样性」可言，不能混进平均值。

    否则单次运行的样本会把均值拉向 1.0，看起来像「很确定」——
    其实是根本没测。
    """
    from agent.run_report import stability

    st = stability([{"sample_id": "s1", "ok": True, "plan_digest": "aaa"}])

    assert st["repeated_samples"] == 0
    assert st["avg_digest_variety"] == 0.0


def test_compare_metrics_exposes_digest_variety() -> None:
    from agent.run_report import compare_metrics

    records = [
        {"sample_id": "s1", "ok": True, "plan_digest": "x", "turns": 1,
         "usage": {"total_tokens": 1}, "duration_ms": 1, "step_count": 1,
         "dag_ok": True, "tool_calls": []},
        {"sample_id": "s1", "ok": True, "plan_digest": "y", "turns": 1,
         "usage": {"total_tokens": 1}, "duration_ms": 1, "step_count": 1,
         "dag_ok": True, "tool_calls": []},
    ]
    assert compare_metrics(records)["digest_variety"] == 2.0


def test_compare_shows_flaky_increase_when_baseline_is_zero() -> None:
    """基线 0 个不稳定样本 → 新臂 1 个，必须显示成「+1」而不是「—」。

    绝对计数指标若用百分比表示变化，基线为 0 时算不出来 ——
    如果这时输出「—」，最该看见的恶化反而看不见了。
    """
    from agent.run_report import render_compare

    baseline = [{"sample_id": "s1", "ok": True}, {"sample_id": "s1", "ok": True}]
    current = [{"sample_id": "s1", "ok": True}, {"sample_id": "s1", "ok": False}]

    text = render_compare("A", "B", baseline, current)
    flaky_line = next(line for line in text.splitlines() if line.startswith("不稳定样本"))

    assert "+1" in flaky_line
    assert "—" not in flaky_line


def test_compare_flags_zero_baseline_for_ratio_metrics() -> None:
    """比例口径（如平均步骤数）基线为 0 时算不出百分比，要明说而不是给「—」。"""
    from agent.run_report import render_compare

    zero = {"step_count": 0, "turns": 0, "usage": {"total_tokens": 0},
            "duration_ms": 0, "dag_ok": True, "tool_calls": [], "ok": True}
    baseline = [dict(zero), dict(zero)]
    current = [dict(zero, step_count=5), dict(zero, step_count=5)]

    text = render_compare("A", "B", baseline, current)
    steps_line = next(line for line in text.splitlines() if line.startswith("平均步骤数"))

    assert "基线为 0" in steps_line
    assert "—" not in steps_line


def test_compare_uses_absolute_delta_for_counts() -> None:
    """绝对计数（运行次数）用绝对差，不用百分比。"""
    from agent.run_report import render_compare

    text = render_compare("A", "B", [_fake_record()], [_fake_record(), _fake_record()])
    runs_line = next(line for line in text.splitlines() if line.startswith("运行次数"))

    assert "+1" in runs_line
    assert "%" not in runs_line


def test_compare_warns_when_records_lack_verdict() -> None:
    """老记录没有 eval_passed 时会静默退回 ok 口径 —— 必须警告。

    不警告的话，「对比表看着有数」会让人误以为结论可信。
    """
    from agent.run_report import render_compare

    # 没有 eval_passed 字段的记录（修复前写的）
    legacy = [{"ok": True}, {"ok": True}]

    text = render_compare("A", "B", legacy, legacy)

    assert "缺少评测判定字段" in text
    assert "结论可能反向" in text


def test_compare_does_not_warn_when_all_records_have_verdict() -> None:
    from agent.run_report import render_compare

    fresh = [{"ok": True, "eval_passed": True}, {"ok": True, "eval_passed": True}]

    text = render_compare("A", "B", fresh, fresh)

    assert "缺少评测判定字段" not in text



# ---------------------------------------------------------------------------
# 评测判定（eval_passed）与 result.ok 是两回事
# ---------------------------------------------------------------------------


def test_build_record_stores_eval_verdict(sample_result) -> None:
    entry = build_record(
        sample_result,
        "任务",
        source="eval",
        eval_passed=False,
        eval_failures=["不应提及「硬性约束」"],
    )

    assert entry["eval_passed"] is False
    assert entry["eval_failures"] == ["不应提及「硬性约束」"]


def test_build_record_eval_verdict_defaults_to_none(sample_result) -> None:
    """非评测来源没有判定，必须是 None（而不是 False）——
    False 会被当成「判定失败」，把 CLI/Web 的运行全算成不通过。"""
    entry = build_record(sample_result, "任务")
    assert entry["eval_passed"] is None


def test_verdict_prefers_eval_passed_over_ok() -> None:
    from agent.run_report import _verdict

    # 产出了合规计划，但违反了期望行为
    assert _verdict({"ok": True, "eval_passed": False}) is False
    # 没产出计划，但期望行为满足（例如「拒绝并说明」类样本）
    assert _verdict({"ok": False, "eval_passed": True}) is True


def test_verdict_falls_back_to_ok() -> None:
    from agent.run_report import _verdict

    assert _verdict({"ok": True, "eval_passed": None}) is True
    assert _verdict({"ok": False, "eval_passed": None}) is False
    assert _verdict({"ok": True}) is True


def test_summarize_uses_verdict_for_success_rate() -> None:
    """success_rate 必须走判定口径，否则对比报告会把方向显示反。"""
    from agent.run_report import summarize

    records = [
        {"ok": True, "eval_passed": False, "tool_calls": [], "usage": {}, "duration_ms": 0},
        {"ok": True, "eval_passed": False, "tool_calls": [], "usage": {}, "duration_ms": 0},
        {"ok": True, "eval_passed": True, "tool_calls": [], "usage": {}, "duration_ms": 0},
        {"ok": False, "eval_passed": True, "tool_calls": [], "usage": {}, "duration_ms": 0},
    ]
    s = summarize(records)

    assert s["success_rate"] == 50.0, "判定通过 2/4"
    assert s["plan_ok_rate"] == 75.0, "合规计划 3/4"
    assert s["verdict_count"] == 2
    assert s["evaluated_count"] == 4


def test_stability_uses_verdict_not_ok() -> None:
    """稳定性也必须按判定算 —— 否则 flaky 会被算错样本。"""
    from agent.run_report import stability

    records = [
        {"sample_id": "s1", "ok": True, "eval_passed": True},
        {"sample_id": "s1", "ok": True, "eval_passed": False},   # 判定层面是不稳定的
    ]
    st = stability(records)

    assert st["flaky"] == 1, "ok 两次都是 True，但判定一次过一次不过"
    assert st["flaky_ids"] == ["s1"]


def test_compare_reflects_verdict_not_ok() -> None:
    """回归：A/B 对比必须用判定口径。

    实测踩坑：temperature 实验里 ``--compare`` 显示「新臂成功率 93.8% < 基线 100%」，
    而真实的判定结果是「新臂 100% > 基线 95.8%」—— **方向完全相反**。
    原因就是报告用了 ``result.ok``（计划能否解析）而不是评测判定。
    """
    from agent.run_report import compare_metrics

    # 基线：计划都能解析，但一条判定失败
    baseline = [
        {"ok": True, "eval_passed": True, "tool_calls": [], "usage": {},
         "duration_ms": 0, "turns": 1, "step_count": 1, "dag_ok": True},
        {"ok": True, "eval_passed": False, "tool_calls": [], "usage": {},
         "duration_ms": 0, "turns": 1, "step_count": 1, "dag_ok": True},
    ]
    # 新臂：有一条没产出计划，但判定全过
    current = [
        {"ok": False, "eval_passed": True, "tool_calls": [], "usage": {},
         "duration_ms": 0, "turns": 1, "step_count": 0, "dag_ok": True},
        {"ok": True, "eval_passed": True, "tool_calls": [], "usage": {},
         "duration_ms": 0, "turns": 1, "step_count": 1, "dag_ok": True},
    ]

    base_m = compare_metrics(baseline)
    cur_m = compare_metrics(current)

    assert base_m["success_rate"] == 50.0
    assert cur_m["success_rate"] == 100.0, "判定口径下新臂更好"
    # 而按 ok 口径看，结论正好相反
    assert base_m["plan_ok_rate"] == 100.0
    assert cur_m["plan_ok_rate"] == 50.0


def test_group_by_warns_when_mixing_verdict_bases() -> None:
    """分组表里混了「有判定」和「无判定」的记录时必须提示。

    老记录缺 `eval_passed` 会静默退回 `ok` 口径，两种口径混在同一列里
    不可直接比较 —— 不提示就会让人以为那一列数字是同一把尺子量出来的。
    """
    from agent.run_report import render

    records = [
        _fake_record(params={"temperature": 0.2}),                      # 无判定
        _fake_record(params={"temperature": 0.0}, eval_passed=True),    # 有判定
    ]
    text = render(records, path=Path("x.jsonl"), group_by="params.temperature")

    assert "缺少评测判定字段" in text


def test_group_by_no_warning_when_all_have_verdict() -> None:
    from agent.run_report import render

    records = [
        _fake_record(params={"temperature": 0.2}, eval_passed=True),
        _fake_record(params={"temperature": 0.0}, eval_passed=True),
    ]
    text = render(records, path=Path("x.jsonl"), group_by="params.temperature")

    assert "缺少评测判定字段" not in text


