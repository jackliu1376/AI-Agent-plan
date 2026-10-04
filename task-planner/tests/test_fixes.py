"""高优先级缺陷的回归测试。

对应 2026-10-03 的代码评审。每个测试对应一条已确认的缺陷，
失败即表示缺陷复现 —— 不要为了「让测试过」而放宽断言。
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from agent.config import Settings
from agent.demo import ScriptedLLM
from agent.loop import Orchestrator
from agent.schema import Plan
from agent.tool_runner import LocalToolRunner
from common.envelope import ERR_BUDGET_EXCEEDED
from mcp_server.tools.attractions import _coverage_note
from mcp_server.tools.base import invoke, tool_retries, tool_settings, tool_timeout
from mcp_server.tools.web import _TextExtractor, fetch_webpage, host_is_blocked
from tests.conftest import make_plan, plan_turn, text_turn, tool_turn

WEATHER = {"city": "成都", "start_date": "2026-10-03", "end_date": "2026-10-04"}


# ===========================================================================
# 缺陷 1：fetch_webpage 标题永远为空（head 在 SKIP_TAGS 里吞掉了 title）
# ===========================================================================


def test_title_extracted_from_head() -> None:
    """真实网页的 <title> 在 <head> 内，必须能解析出来。"""
    html = (
        "<html><head><title>成都旅游攻略_马蜂窝</title></head>"
        "<body><h1>成都必去景点</h1><p>宽窄巷子很值得去。</p></body></html>"
    )
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    assert parser.title == "成都旅游攻略_马蜂窝"
    assert "宽窄巷子" in parser.get_text()
    assert parser.unbalanced_skip is False


def test_skip_stack_survives_malformed_html() -> None:
    """标签错位嵌套时，跳过状态不能永久卡住。"""
    html = (
        "<html><body><h1>标题</h1>"
        "<script>var x=1;</script>"
        "<p>第一段</p><style>a{}</style><p>第二段</p>"
        "</body></html>"
    )
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    text = parser.get_text()
    assert "第一段" in text
    assert "第二段" in text
    assert parser.unbalanced_skip is False


def test_unclosed_script_is_flagged() -> None:
    """未闭合 script 会导致正文丢失，此时必须给出警告标记而不是静默返回。"""
    html = "<html><body><h1>标题</h1><script>var x=1;<p>正文</p></body></html>"
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()

    assert parser.unbalanced_skip is True


# ===========================================================================
# 缺陷 2：SSRF —— 内网 / 元数据地址必须被拒绝
# ===========================================================================


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",  # AWS / 阿里云元数据端点
        "0.0.0.0",
        "::1",
        "metadata.google.internal",
    ],
)
def test_private_hosts_are_blocked(host: str) -> None:
    assert host_is_blocked(host) is not None, f"{host} 应被拒绝"


def test_fetch_webpage_rejects_metadata_endpoint() -> None:
    result = invoke(
        "fetch_webpage",
        {"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert result.ok is False
    assert "拒绝访问" in result.error.message


def test_fetch_webpage_rejects_localhost() -> None:
    result = invoke("fetch_webpage", {"url": "http://127.0.0.1:8000/api/health"})
    assert result.ok is False
    assert "拒绝访问" in result.error.message


# ---------------------------------------------------------------------------
# 端到端：对着本地 mock 服务跑完整抓取链路
# ---------------------------------------------------------------------------


@pytest.fixture
def local_site(monkeypatch: pytest.MonkeyPatch):
    """起一个本地 HTTP 服务，验证抓取链路的真实行为。

    需要 ``ALLOW_PRIVATE_URLS=1`` 才能访问 127.0.0.1 —— 这也顺带证明了
    该开关确实生效。
    """
    import http.server
    import socketserver
    import threading

    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "1")

    PAGE = (
        "<html><head><title>成都旅游攻略_马蜂窝</title>"
        "<meta name='description' content='x'></head>"
        "<body><h1>成都必去景点</h1><p>宽窄巷子很值得去。</p>"
        "<script>var a=1;</script><p>锦里也不错。</p></body></html>"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/page")
                self.end_headers()
                return
            if self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
                return
            if self.path == "/empty":
                body = b"<html><head><title>Empty</title></head><body></body></html>"
                self._send(200, "text/html", body)
                return
            if self.path == "/page":
                self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_local_page_extracts_title(local_site: str) -> None:
    """端到端：真实 HTTP 响应下，标题与正文都要能正确解析。"""
    result = invoke("fetch_webpage", {"url": f"{local_site}/page"})

    assert result.ok, result.error.message if not result.ok else ""
    assert result.data["title"] == "成都旅游攻略_马蜂窝"
    assert "宽窄巷子" in result.data["text"]
    assert "锦里" in result.data["text"]
    assert "var a=1" not in result.data["text"], "script 内容不该混进正文"


def test_fetch_follows_redirect(local_site: str) -> None:
    result = invoke("fetch_webpage", {"url": f"{local_site}/redirect"})
    assert result.ok, result.error.message if not result.ok else ""
    assert result.data["title"] == "成都旅游攻略_马蜂窝"


def test_fetch_stops_redirect_loop(local_site: str) -> None:
    result = invoke("fetch_webpage", {"url": f"{local_site}/loop"})
    assert result.ok is False
    assert "重定向次数" in result.error.message


def test_fetch_reports_empty_body(local_site: str) -> None:
    """抽不到正文时必须报错，而不是返回「空的成功结果」。"""
    result = invoke("fetch_webpage", {"url": f"{local_site}/empty"})
    assert result.ok is False
    assert "未能从页面提取到正文" in result.error.message


def test_fetch_handles_404(local_site: str) -> None:
    result = invoke("fetch_webpage", {"url": f"{local_site}/nope"})
    assert result.ok is False
    assert "404" in result.error.message


def test_private_urls_allowed_only_with_flag(
    local_site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉开关后，同一个地址必须被拒 —— 证明拦截确实生效。"""
    monkeypatch.delenv("ALLOW_PRIVATE_URLS", raising=False)
    result = invoke("fetch_webpage", {"url": f"{local_site}/page"})
    assert result.ok is False
    assert "拒绝访问" in result.error.message


# ===========================================================================
# 缺陷 3：空计划被判定为成功
# ===========================================================================


def test_plan_without_steps_is_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要 1 个步骤"):
        Plan.model_validate({"goal": "空计划", "phases": []})


def test_plan_with_empty_phase_is_rejected() -> None:
    with pytest.raises(ValueError, match="至少需要 1 个步骤"):
        Plan.model_validate({"goal": "空阶段", "phases": [{"name": "阶段一", "steps": []}]})


def test_empty_plan_triggers_repair(settings: Settings, runner) -> None:
    """空计划会被结构校验拦下，进而触发修复重试（而不是被当成成功）。"""
    empty = make_plan()
    empty["phases"] = []
    llm = ScriptedLLM([plan_turn(empty), plan_turn(make_plan())])
    result = Orchestrator(llm, runner, settings).run("空计划测试")

    assert result.trace.repairs == 1
    assert result.ok is True


# ===========================================================================
# 缺陷 4：重复 step id 被静默去重
# ===========================================================================


def test_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="必须全局唯一"):
        Plan.model_validate(
            {
                "goal": "重复 id",
                "phases": [
                    {
                        "name": "阶段一",
                        "steps": [
                            {"id": "S1", "action": "动作A"},
                            {"id": "S1", "action": "动作B"},
                        ],
                    }
                ],
            }
        )


def test_blank_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="action 为空"):
        Plan.model_validate(
            {"goal": "空动作", "phases": [{"name": "P", "steps": [{"id": "S1", "action": "  "}]}]}
        )


# ===========================================================================
# 缺陷 5：循环依赖的计划 ok=True
# ===========================================================================


def test_cyclic_dependency_makes_result_not_ok(settings: Settings, runner) -> None:
    cyclic = make_plan()
    cyclic["phases"] = [
        {
            "name": "阶段一",
            "steps": [
                {"id": "S1", "action": "A", "depends_on": ["S2"]},
                {"id": "S2", "action": "B", "depends_on": ["S1"]},
            ],
        }
    ]
    result = Orchestrator(ScriptedLLM([plan_turn(cyclic)]), runner, settings).run("环依赖测试")

    assert result.plan is not None, "计划本身能解析"
    assert result.trace.dag_ok is False
    assert result.trace.output_status == "invalid_dag"
    assert result.ok is False, "含循环依赖的计划不可执行，不能判定为成功"
    assert "循环依赖" in result.trace.dag_error


def test_self_dependency_is_rejected() -> None:
    plan = Plan.model_validate(
        {
            "goal": "自依赖",
            "phases": [{"name": "P", "steps": [{"id": "S1", "action": "A", "depends_on": ["S1"]}]}],
        }
    )
    ok, msg = plan.check_dag()
    assert ok is False
    assert "依赖了自己" in msg


def test_long_dependency_chain_does_not_overflow() -> None:
    """迭代式 DFS：1000+ 步的链式依赖不应触发递归深度限制。"""
    steps = [
        {"id": f"S{i}", "action": f"动作{i}", "depends_on": [f"S{i - 1}"] if i else []}
        for i in range(1500)
    ]
    plan = Plan.model_validate({"goal": "长链", "phases": [{"name": "P", "steps": steps}]})
    ok, msg = plan.check_dag()
    assert ok, msg


# ===========================================================================
# 缺陷 6：预算门控排在缓存之前
# ===========================================================================


def test_cache_hit_works_after_budget_exhausted(
    settings: Settings, fake_weather, runner
) -> None:
    """预算耗尽后，重复调用同一工具应命中缓存拿到数据，而不是被拒。"""
    tight = dataclasses.replace(settings, tool_budget=1)
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER)),
            tool_turn(("c2", "get_weather_forecast", WEATHER)),  # 同参数，应命中缓存
            plan_turn(make_plan()),
        ]
    )
    result = Orchestrator(llm, runner, tight).run("缓存测试")

    records = result.trace.tool_calls
    assert len(records) == 2
    assert records[1].cached is True, "第二次应命中缓存"
    assert records[1].ok is True, "命中缓存应拿到数据，而不是预算错误"
    assert result.trace.budget_exceeded is False, "缓存命中不该消耗预算"


# ===========================================================================
# 缺陷 7：预算拒绝的调用不记入 trace
# ===========================================================================


def test_budget_rejection_is_recorded_in_trace(settings: Settings, fake_weather, runner) -> None:
    """模型请求几次，trace 就该记几次 —— 否则所有基于 trace 的指标都偏低。"""
    tight = dataclasses.replace(settings, tool_budget=1)
    llm = ScriptedLLM(
        [
            tool_turn(
                ("c1", "get_weather_forecast", WEATHER),
                ("c2", "query_attractions_db", {"city": "成都"}),  # 预算耗尽，应被拒
            ),
            plan_turn(make_plan()),
        ]
    )
    result = Orchestrator(llm, runner, tight).run("预算测试")

    records = result.trace.tool_calls
    assert len(records) == 2, "模型请求了 2 次，trace 必须记 2 次"
    assert records[1].ok is False
    assert records[1].error_code == ERR_BUDGET_EXCEEDED
    assert result.trace.budget_exceeded is True


# ===========================================================================
# 缺陷 8：run_stream 无异常兜底
# ===========================================================================


class _ExplodingLLM:
    def chat(self, messages, tools=None):  # noqa: ANN001
        raise RuntimeError("上游炸了")


async def test_llm_exception_becomes_error_event(settings: Settings, runner) -> None:
    """LLM 抛异常必须转成 error 事件 + 失败结果，不能裸抛给调用方。"""
    orch = Orchestrator(_ExplodingLLM(), runner, settings)
    events = [event async for event in orch.run_stream("测试")]

    types = [e.type for e in events]
    assert "error" in types
    assert types[-1] == "plan_ready"

    error_event = next(e for e in events if e.type == "error")
    assert "上游炸了" in error_event.data["message"]

    assert orch.last_result is not None
    assert orch.last_result.ok is False
    assert orch.last_result.trace.stop_reason == "runtime_error"
    assert orch.last_result.trace.output_status == "runtime_error"


async def test_exception_still_produces_final_event(settings: Settings, runner) -> None:
    """即使异常，也必须产出 plan_ready，否则 SSE 客户端会一直等下去。"""
    orch = Orchestrator(_ExplodingLLM(), runner, settings)
    events = [event async for event in orch.run_stream("测试")]

    assert events[-1].type == "plan_ready"
    assert events[-1].data["ok"] is False


# ===========================================================================
# 缺陷 9：turns 语义不一致（含修复重试）
# ===========================================================================


def test_repair_does_not_inflate_turns(settings: Settings, runner) -> None:
    """trace.turns 只表示模型轮次，不能把修复重试算进去。"""
    tight = dataclasses.replace(settings, max_turns=1)
    llm = ScriptedLLM([text_turn("没有 JSON"), plan_turn(make_plan())])
    result = Orchestrator(llm, runner, tight).run("修复测试")

    assert result.trace.turns == 1, "只调用了 1 次模型（修复重试不算轮次）"
    assert result.trace.repairs == 1
    assert result.ok is True


def test_stop_reason_and_output_status_are_separate(settings: Settings, fake_weather, runner) -> None:
    """轮次耗尽但最终产出合法计划 —— 这是成功，不能因停止原因判为失败。"""
    tight = dataclasses.replace(settings, max_turns=1, tool_budget=5)
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER)),
            plan_turn(make_plan()),  # 修复重试拿到计划
        ]
    )
    result = Orchestrator(llm, runner, tight).run("语义测试")

    assert result.trace.stop_reason == "max_turns_reached"
    assert result.trace.output_status == "ok"
    assert result.ok is True


# ===========================================================================
# 缺陷 10：max_turns 耗尽 + final_text 为空 → 跳过修复重试
# ===========================================================================


def test_empty_output_still_triggers_repair(settings: Settings, fake_weather, runner) -> None:
    """模型全程只调工具、从未返回文本时，也要给它一次「请现在输出」的机会。"""
    tight = dataclasses.replace(settings, max_turns=1, tool_budget=5)
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "get_weather_forecast", WEATHER)),
            plan_turn(make_plan()),
        ]
    )
    result = Orchestrator(llm, runner, tight).run("空文本测试")

    assert result.trace.repairs == 1, "final_text 为空也必须触发修复重试"
    assert result.ok is True


# ===========================================================================
# 缺陷 11：配置死字段 / 非法值静默接受
# ===========================================================================


def test_tool_settings_override_takes_effect() -> None:
    """Settings 注入的超时必须真正生效（此前是死字段）。"""
    assert tool_timeout() == 10  # 默认
    with tool_settings(timeout=3, retries=7):
        assert tool_timeout() == 3
        assert tool_retries() == 7
    assert tool_timeout() == 10  # 退出后恢复


def test_orchestrator_injects_settings_into_tools(
    settings: Settings, fake_weather, runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编排循环必须把 Settings 里的超时/重试注入到工具层。"""
    seen: dict[str, int] = {}

    from agent import tool_runner as tr_module
    from mcp_server.tools.base import invoke as real_invoke

    def spy(name: str, args=None, *, retries=None):  # noqa: ANN001
        seen["timeout"] = tool_timeout()
        seen["retries"] = tool_retries()
        return real_invoke(name, args, retries=retries)

    # 注意要打在 tool_runner 的命名空间上 —— 它用 from ... import 引入了 invoke
    monkeypatch.setattr(tr_module, "invoke", spy)

    tight = dataclasses.replace(settings, tool_timeout=42, tool_retries=3)
    orch = Orchestrator(
        ScriptedLLM(
            [
                tool_turn(("c1", "get_weather_forecast", WEATHER)),
                plan_turn(make_plan()),
            ]
        ),
        LocalToolRunner(),
        tight,
    )
    orch.run("注入测试")

    assert seen.get("timeout") == 42, "Settings.tool_timeout 未注入工具层"
    assert seen.get("retries") == 3, "Settings.tool_retries 未注入工具层"


def test_invalid_max_turns_is_not_silently_accepted() -> None:
    """max_turns=0 不该静默回落到默认值，也不该一轮不跑就返回空结果。"""
    settings = Settings(
        api_key="k",
        base_url="https://api.deepseek.com",
        model="m",
        max_turns=0,
        tool_budget=1,
        tool_timeout=5,
        tool_retries=0,
        prompt_path=Settings.load().prompt_path,
    )
    result = Orchestrator(
        ScriptedLLM([plan_turn(make_plan())]), LocalToolRunner(), settings
    ).run("非法轮次")

    # 强制按 1 处理，因此模型仍被调用了一次并产出了计划
    assert result.trace.turns == 1
    assert result.ok is True


def test_env_int_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_server.tools.base import env_int

    monkeypatch.setenv("AUDIT_TEST_INT", "-5")
    assert env_int("AUDIT_TEST_INT", 8, minimum=1) == 8
    monkeypatch.setenv("AUDIT_TEST_INT", "not-a-number")
    assert env_int("AUDIT_TEST_INT", 8, minimum=1) == 8
    monkeypatch.setenv("AUDIT_TEST_INT", "3")
    assert env_int("AUDIT_TEST_INT", 8, minimum=1) == 3


# ===========================================================================
# 缺陷 12：过时的工具描述会误导模型
# ===========================================================================


def test_coverage_note_matches_actual_data() -> None:
    """工具描述里的城市数必须与数据库一致 —— 它直接进 LLM 的 tools 参数。"""
    from mcp_server.data.seed import covered_cities

    note = _coverage_note()
    actual = len(covered_cities())

    assert note, "覆盖说明不应为空"
    assert f"{actual} 个城市" in note, f"描述与实数据不符：{note}（实际 {actual} 城）"


def test_attractions_description_is_not_stale() -> None:
    """曾经写着「5 国内 + 7 国际」，实际已 42 城 —— 防止再次漂移。"""
    from mcp_server.tools.base import TOOL_REGISTRY

    description = TOOL_REGISTRY["query_attractions_db"].description
    assert "成都/杭州/北京/西安/三亚" not in description
    assert "当地货币" in description


def test_max_price_description_warns_about_currency() -> None:
    from mcp_server.tools.attractions import AttractionsParams

    field = AttractionsParams.model_fields["max_price"]
    assert field.description is not None
    assert "当地货币" in field.description


# ===========================================================================
# 数据时效标注（第 1 层）+ 官方核实渠道（第 2 层）
# ===========================================================================


def test_every_seed_row_has_verified_at() -> None:
    """时效性字段必须带核对时间 —— 否则用户无法判断数据有多旧。"""
    from mcp_server.data.seed import COLUMNS, DATA_VERIFIED_AT, SEED_ROWS

    idx = COLUMNS.index("verified_at")
    for row in SEED_ROWS:
        assert row[idx], f"缺少 verified_at: {row}"
        assert row[idx] == DATA_VERIFIED_AT


def test_tool_returns_freshness_metadata(fake_weather) -> None:
    """工具必须把时效信息交给模型，否则模型无从转述。"""
    result = invoke("query_attractions_db", {"city": "成都", "limit": 2})

    assert result.ok, result.error.message if not result.ok else ""
    data = result.data
    assert data["data_verified_at"]
    assert "核对的演示数据" in data["freshness_note"]
    assert data["official_channel"]
    # 每条记录也要带核对时间
    assert all(item["verified_at"] == data["data_verified_at"] for item in data["items"])


def test_official_channel_differs_for_domestic_and_international() -> None:
    from mcp_server.tools.attractions import official_channel_for

    cn = official_channel_for("成都", "中国")
    intl = official_channel_for("东京", "日本")

    assert "微信" in cn, "国内景区售票预约主要走微信渠道"
    assert "官网" in intl
    assert cn != intl


def test_official_channel_has_no_fabricated_urls() -> None:
    """渠道字段只描述渠道类型，不写具体网址 —— 编造网址正是本项目要避免的。"""
    from mcp_server.tools.attractions import CHANNEL_CN, CHANNEL_INTL

    for text in (CHANNEL_CN, CHANNEL_INTL):
        assert "http" not in text
        assert "www." not in text


# -- schema 层：引用了时效性数据源就必须交代 ------------------------------


def test_plan_using_attractions_requires_freshness() -> None:
    plan = make_plan(data_freshness="", verification_channels=["某渠道"])
    plan["phases"] = [
        {"name": "P", "steps": [{"id": "S1", "action": "查景点", "tool": "query_attractions_db"}]}
    ]
    with pytest.raises(ValueError, match="data_freshness 不能为空"):
        Plan.model_validate(plan)


def test_plan_using_weather_requires_verification_channels() -> None:
    plan = make_plan(data_freshness="天气为实时预报", verification_channels=[])
    with pytest.raises(ValueError, match="verification_channels 不能为空"):
        Plan.model_validate(plan)


def test_plan_without_freshness_tools_needs_no_declaration() -> None:
    """纯流程类任务（写论文）不该被强加一句无意义的时效声明。"""
    plan = make_plan(data_freshness="", verification_channels=[])
    plan["phases"] = [
        {
            "name": "P",
            "steps": [
                {"id": "S1", "action": "列提纲", "tool": None, "tool_args": {}},
                {"id": "S2", "action": "写初稿", "tool": None, "tool_args": {}},
            ],
        }
    ]
    parsed = Plan.model_validate(plan)
    assert parsed.data_freshness == ""
    assert parsed.verification_channels == []


def test_missing_freshness_triggers_repair(settings: Settings, fake_weather, runner) -> None:
    """缺时效声明 → 校验失败 → 修复重试 → 拿到合规计划。"""
    bad = make_plan(data_freshness="")
    llm = ScriptedLLM(
        [
            tool_turn(("c1", "query_attractions_db", {"city": "成都"})),
            plan_turn(bad),
            plan_turn(make_plan()),
        ]
    )
    result = Orchestrator(llm, runner, settings).run("成都景点")

    assert result.trace.repairs == 1
    assert result.ok is True
    assert result.plan is not None
    assert result.plan.data_freshness


def test_freshness_reaches_event_payload(settings: Settings, fake_weather, runner) -> None:
    """时效信息要能传到前端（SSE 事件里必须带上）。"""
    llm = ScriptedLLM([plan_turn(make_plan())])
    result = Orchestrator(llm, runner, settings).run("成都景点")

    payload = result.to_event_data()
    assert payload["plan"]["data_freshness"]
    assert payload["plan"]["verification_channels"]


def test_coverage_note_does_not_hardcode_city_count() -> None:
    """种子库文档/描述里不该再出现写死的城市数字（曾因此漂移过两次）。"""
    from mcp_server.tools.attractions import _coverage_note

    note = _coverage_note()
    # 描述里的数字必须等于实际城市数
    from mcp_server.data.seed import covered_cities

    assert f"{len(covered_cities())} 个城市" in note
