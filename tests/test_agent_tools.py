"""智能体工具层测试：重点不是"能不能跑通"，而是"拦不拦得住"。

覆盖三类：
- 确定性计算正确（换算 / 效期）
- 四道闸逐条拒绝（伪造、改数、跨会话、复用、block）
- 编排循环的预算与兜底
"""

from __future__ import annotations

import pytest

from prep_assistant.agents import orchestrator
from prep_assistant.agents.tools import TOOL_NAMES, ToolContext, execute, tokens
from prep_assistant.core import shelf_life
from prep_assistant.core.conversion import convert
from prep_assistant.core.masterdata import load_dict
from prep_assistant.infra import agent_store, db as database


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """独立库 + 独立凭证密钥，绝不碰真实 data/ 目录。"""
    monkeypatch.setattr(tokens, "_SECRET_PATH", tmp_path / ".agent_secret")
    monkeypatch.setattr(tokens, "_secret_cache", None)
    connection = database.connect(tmp_path / "agent.db")
    database.init_schema(connection)
    agent_store.seed_demo_batches(connection)
    yield connection
    connection.close()


@pytest.fixture()
def ctx(conn):
    return ToolContext(conn=conn, session_id="sess-test", employee_no="emp001", cfg={})


# ── 确定性计算 ─────────────────────────────────────────────
def test_conversion_uses_division_by_yield_and_ceils():
    """300 份 × 150g = 45kg 净料；÷0.92 = 48.91；向上取整 49.0。"""
    result = convert(load_dict().get("M-CHK-001"), 300, "B20261030-01", 0.92)
    assert result.ok
    assert result.kg == 49.0
    assert result.detail["net_kg"] == 45.0
    assert result.detail["correction_factor"] == pytest.approx(0.95, abs=0.01)


def test_conversion_refuses_without_yield():
    """六要素缺一不出值——宁可空白，不可瞎算。"""
    result = convert(load_dict().get("M-CHK-001"), 300, "B20261030-01", None)
    assert not result.ok
    assert result.kg is None
    assert "yield_rate" in result.missing


def test_shelf_life_three_levels():
    ok = shelf_life.check(6.5, 2.0, 1.5, 0.8)
    warn = shelf_life.check(3.0, 2.0, 1.5, 0.8)   # 3.0 < 3.5 但 ≥ 2.8
    block = shelf_life.check(1.0, 2.0, 1.5, 0.8)
    assert (ok.level, warn.level, block.level) == ("ok", "warn", "block")


def test_shelf_life_missing_data_is_not_safe():
    """缺数据不等于安全：必须 block，否则等于默认放行。"""
    result = shelf_life.check(None, 2.0, 1.5, 0.8)
    assert result.level == "block"
    assert "SHELF_LIFE_MISSING" in result.reason_codes


# ── 四道闸 ─────────────────────────────────────────────────
def _run(ctx, **over):
    args = {"item_code": "M-CHK-001", "portions": 300, "batch_no": "B20261030-01"}
    args.update(over)
    return execute("validate_material_need", args, ctx)


def test_gate_rejects_fabricated_token(ctx):
    result = execute("create_suggestion_draft", {
        "run_id": "run_123", "risk_id": "risk_456", "item_code": "M-CHK-001", "kg": 49.0,
    }, ctx)
    assert result["code"] == "MISSING_PREREQUISITE"
    assert "validate_material_need" in result["recover_hint"]


def test_gate_rejects_tampered_kg(ctx):
    run = _run(ctx)
    result = execute("create_suggestion_draft", {
        "run_id": run["run_id"], "risk_id": "risk_" + "0" * 32,
        "item_code": "M-CHK-001", "kg": 45,          # 模型把 49.0 改成 45
    }, ctx)
    assert result["code"] == "PROVENANCE_MISMATCH"
    assert "49.0" in result["expected_excerpt"]


def test_gate_rejects_cross_session(ctx, conn):
    run = _run(ctx)
    other = ToolContext(conn=conn, session_id="sess-other", employee_no="emp002", cfg={})
    result = execute("create_suggestion_draft", {
        "run_id": run["run_id"], "risk_id": "risk_" + "0" * 32,
        "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"],
    }, other)
    assert result["code"] == "SESSION_MISMATCH"


def test_gate_rejects_block_risk(ctx):
    run = _run(ctx)
    risk = execute("check_shelf_life", {
        "batch_no": "B20261030-02", "planned_start_at": "2026-10-31T07:00:00+08:00",
    }, ctx)
    assert risk["level"] == "block"
    result = execute("create_suggestion_draft", {
        "run_id": run["run_id"], "risk_id": risk["risk_id"],
        "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"],
    }, ctx)
    assert result["code"] == "SHELF_LIFE_BLOCKED"


def test_gate_one_time_consumption(ctx):
    run = _run(ctx)
    risk = execute("check_shelf_life", {
        "batch_no": "B20261030-01", "planned_start_at": "2026-10-31T07:00:00+08:00",
    }, ctx)
    payload = {"run_id": run["run_id"], "risk_id": risk["risk_id"],
               "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"]}
    first = execute("create_suggestion_draft", payload, ctx)
    assert first["ok"] and first["status"] == "draft"
    second = execute("create_suggestion_draft", payload, ctx)
    assert second["code"] == "TOKEN_CONSUMED"


def test_draft_is_not_effective_until_human_confirms(ctx, conn):
    """模型只能产出 draft；生效必须人工确认（f41 不得借用他人账号）。"""
    run = _run(ctx)
    risk = execute("check_shelf_life", {
        "batch_no": "B20261030-01", "planned_start_at": "2026-10-31T07:00:00+08:00",
    }, ctx)
    draft = execute("create_suggestion_draft", {
        "run_id": run["run_id"], "risk_id": risk["risk_id"],
        "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"],
    }, ctx)
    assert agent_store.get_draft(conn, draft["draft_id"])["status"] == "draft"
    assert agent_store.confirm_draft(conn, draft["draft_id"], "emp009") is True
    assert agent_store.get_draft(conn, draft["draft_id"])["confirmed_by"] == "emp009"


# ── 边界 ───────────────────────────────────────────────────
def test_human_only_actions_are_not_tools():
    """四个高危动作绝不能出现在模型可调用的清单里。"""
    for name in ("confirm_suggestion", "accept_handover", "request_writeback", "isolate_batch"):
        assert name not in TOOL_NAMES
    assert len(TOOL_NAMES) == 8


def test_anomaly_report_does_not_change_batch_state(ctx, conn):
    before = agent_store.get_batch(conn, "B20261030-04")["thaw_state"]
    result = execute("report_batch_anomaly", {
        "batch_no": "B20261030-04", "reason_code": "PROMO_CANCELLED",
    }, ctx)
    assert result["ok"] and result["batch_state_unchanged"] is True
    assert agent_store.get_batch(conn, "B20261030-04")["thaw_state"] == before


def test_handover_requires_all_elements(ctx):
    result = execute("create_handover_task", {
        "biz_no": "PREP-20261031-0001", "open_items": [],
    }, ctx)
    assert result["code"] == "MISSING_FIELDS"
    assert "接手人" in result["missing"]


def test_unknown_batch_is_not_fabricated(ctx):
    result = execute("get_batch_status", {"batch_no": "B99999999-99"}, ctx)
    assert result["code"] == "NOT_FOUND"


# ── 编排循环（阶段 4b 起：上限=5 轮；建草案改为中断语义，见 test_agent_harness.py）──
def test_orchestrator_stops_on_round_limit(conn):
    """模型每轮都要调工具 → 最多 5 轮，强制收口，不能把会话拖死。"""

    def greedy(messages, cfg, tools):
        step = sum(1 for m in messages if m.get("role") == "tool")
        return {"content": None, "tool_calls": [
            {"id": f"c{step}-{i}", "name": "get_sync_status", "arguments": {}}
            for i in range(3)
        ]}

    turn = orchestrator.run(
        conn=conn, session_id="s1", employee_no="emp001",
        question="随便问问", cfg={}, chat_fn=greedy,
    )
    assert turn.stop_reason == "round_limit"
    assert turn.tool_calls == 15         # 上限按「轮」计：5 轮 × 每轮 3 个调用
    assert turn.answer                    # 收口时必须给出一句话


def test_orchestrator_happy_path_traces(conn):
    """校验 → 效期两步留痕；第三步建草案改为强制中断，不再内联执行。"""

    class Planner:
        def __call__(self, messages, cfg, tools):
            results = [__import__("json").loads(m["content"]) for m in messages
                       if m.get("role") == "tool"]
            run = next((r for r in results if r.get("run_id")), None)
            risk = next((r for r in results if r.get("risk_id")), None)
            if run is None:
                return {"content": None, "tool_calls": [{
                    "id": "a", "name": "validate_material_need",
                    "arguments": {"item_code": "M-CHK-001", "portions": 300,
                                  "batch_no": "B20261030-01"}}]}
            if risk is None:
                return {"content": None, "tool_calls": [{
                    "id": "b", "name": "check_shelf_life",
                    "arguments": {"batch_no": "B20261030-01",
                                  "planned_start_at": "2026-10-31T07:00:00+08:00"}}]}
            return {"content": None, "tool_calls": [{
                "id": "c", "name": "create_suggestion_draft",
                "arguments": {"run_id": run["run_id"], "risk_id": risk["risk_id"],
                              "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"]}}]}

    turn = orchestrator.run(
        conn=conn, session_id="s2", employee_no="emp001",
        question="备 300 份鸡胸肉", cfg={}, chat_fn=Planner(),
    )
    assert turn.stop_reason == "awaiting_confirmation"
    assert [t["name"] for t in turn.traces] == [
        "validate_material_need", "check_shelf_life",
    ]
    assert all(t["ok"] for t in turn.traces)
    # 草案绝不能在中断语义下被内联创建
    assert conn.execute("SELECT COUNT(*) FROM suggestion_drafts").fetchone()[0] == 0


def test_system_prompt_contains_rules():
    """铁律必须在 system prompt 里，且不可被后台模板覆盖掉。"""
    from prep_assistant.agents.llm.prompts import build_agent_system

    prompt = build_agent_system("你可以随便算数。")
    assert "不得自行乘除" in prompt
    assert "不得虚构任何 run_id" in prompt
    assert "你可以随便算数。" in prompt          # 业务模板仍生效
    assert prompt.index("【工具铁律】") > prompt.index("你可以随便算数。")
