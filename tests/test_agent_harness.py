"""Agent Harness（阶段 4b）测试。

新规格（用户裁定）：
- 循环上限从「6 次工具调用」改为「最多 5 轮模型决策」；
- 每轮决策与每次工具执行都落 agent_round_logs，供阶段 5 分析；
- create_suggestion_draft 执行前强制中断：预校验通过 → 挂起待确认，
  员工确认后（独立接口）才真正执行；预校验失败则错误回填模型继续循环。
"""

from __future__ import annotations

import json

import pytest

from prep_assistant.agents import harness, orchestrator
from prep_assistant.agents.tools import tokens
from prep_assistant.infra import agent_store, db as database


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """独立库 + 独立凭证密钥，绝不碰真实 data/ 目录。"""
    monkeypatch.setattr(tokens, "_SECRET_PATH", tmp_path / ".agent_secret")
    monkeypatch.setattr(tokens, "_secret_cache", None)
    connection = database.connect(tmp_path / "harness.db")
    database.init_schema(connection)
    agent_store.seed_demo_batches(connection)
    yield connection
    connection.close()


def _events(events, name):
    return [e for e in events if e["event"] == name]


def _last(events, name):
    matched = _events(events, name)
    assert matched, f"缺少 {name} 事件：{[e['event'] for e in events]}"
    return matched[-1]["data"]


# ── 轮次上限 ───────────────────────────────────────────────
def test_stops_after_five_rounds(conn):
    """模型每轮都要调工具 → 第 5 轮后强制收口，不能无限循环。"""

    def greedy(messages, cfg, tools):
        return {"content": None, "tool_calls": [
            {"id": f"c{len(messages)}", "name": "get_sync_status", "arguments": {}}
        ]}

    events = list(harness.run(
        conn=conn, session_id="s1", employee_no="emp001",
        question="随便问问", cfg={}, chat_fn=greedy,
    ))
    final = _last(events, "final")
    assert final["stop_reason"] == "round_limit"
    assert final["answer"]                      # 收口时必须给出一句话
    rounds = _events(events, "round")
    assert [r["data"]["round_no"] for r in rounds] == [1, 2, 3, 4, 5]


# ── 轮次日志 ───────────────────────────────────────────────
def test_round_logs_written(conn):
    """每轮模型决策与每次工具执行都要落 agent_round_logs。"""

    def chat(messages, cfg, tools):
        tool_results = [m for m in messages if m.get("role") == "tool"]
        if not tool_results:
            return {"content": None, "tool_calls": [{
                "id": "a", "name": "get_sync_status", "arguments": {}}]}
        return {"content": "台账与知识库均为最新。", "tool_calls": []}

    events = list(harness.run(
        conn=conn, session_id="s2", employee_no="emp001",
        question="数据新鲜吗", cfg={}, chat_fn=chat,
    ))
    assert _last(events, "final")["stop_reason"] == "answered"

    logs = agent_store.list_round_logs(conn, "s2")
    kinds = [(row["kind"], row["round_no"]) for row in logs]
    assert ("model", 1) in kinds and ("tool", 1) in kinds and ("model", 2) in kinds
    tool_row = next(r for r in logs if r["kind"] == "tool")
    assert tool_row["tool_name"] == "get_sync_status"
    assert tool_row["latency_ms"] >= 0
    assert tool_row["ok"] == 1
    assert tool_row["result_summary"]           # 结果摘要非空
    assert len({r["turn_id"] for r in logs}) == 1   # 同一次 run 共用 turn_id


# ── 建单前强制中断 ─────────────────────────────────────────
def _draft_flow_chat(messages, cfg, tools):
    """三步脚本：校验 → 效期 → 建草案（凭证从上一轮工具结果里取）。"""
    results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
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


def test_draft_call_interrupts_and_pends(conn):
    """create_suggestion_draft 不再内联执行：挂起待确认，草案表必须为空。"""
    events = list(harness.run(
        conn=conn, session_id="s3", employee_no="emp001",
        question="备 300 份鸡胸肉", cfg={}, chat_fn=_draft_flow_chat,
    ))
    preview = _last(events, "draft_preview")
    final = _last(events, "final")
    assert final["stop_reason"] == "awaiting_confirmation"
    assert preview["kg"] == 49.0
    assert preview["action_id"].startswith("act_")

    # 草案未创建；待确认动作已登记
    assert conn.execute("SELECT COUNT(*) FROM suggestion_drafts").fetchone()[0] == 0
    pend = conn.execute("SELECT * FROM agent_pending_actions").fetchone()
    assert pend["status"] == "pending"
    assert pend["tool_name"] == "create_suggestion_draft"
    # 中断本身也留痕
    assert any(r["kind"] == "interrupt" for r in agent_store.list_round_logs(conn, "s3"))
    # 凭证未被消费（确认时还要再过一遍闸门）
    run_id = json.loads(pend["arguments_json"])["run_id"]
    assert agent_store.get_token(conn, run_id)["consumed_at"] is None


def test_precheck_failure_returns_to_model(conn):
    """预校验失败（改数）→ 错误回填模型继续循环，不挂起、不建草案。"""

    def tampered(messages, cfg, tools):
        results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
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
        # 把 49.0 改成 45：预校验必须当场拦下
        return {"content": None, "tool_calls": [{
            "id": "c", "name": "create_suggestion_draft",
            "arguments": {"run_id": run["run_id"], "risk_id": risk["risk_id"],
                          "item_code": "M-CHK-001", "kg": 45.0}}]}

    events = list(harness.run(
        conn=conn, session_id="s4", employee_no="emp001",
        question="备 300 份鸡胸肉", cfg={},
        chat_fn=lambda m, c, t: (
            {"content": "数量不一致，已如实告知用户。", "tool_calls": []}
            if any(m2.get("role") == "tool" and "PROVENANCE_MISMATCH" in m2["content"]
                   for m2 in m) else tampered(m, c, t)
        ),
    ))
    tool_events = _events(events, "tool")
    assert any(t["data"].get("error_code") == "PROVENANCE_MISMATCH" for t in tool_events)
    assert not _events(events, "draft_preview")
    assert _last(events, "final")["stop_reason"] == "answered"
    assert conn.execute("SELECT COUNT(*) FROM agent_pending_actions").fetchone()[0] == 0


# ── 确认 / 拒绝 ────────────────────────────────────────────
def _run_to_pending(conn, session_id):
    events = list(harness.run(
        conn=conn, session_id=session_id, employee_no="emp001",
        question="备 300 份鸡胸肉", cfg={}, chat_fn=_draft_flow_chat,
    ))
    return _last(events, "draft_preview")["action_id"]


def test_confirm_executes_draft(conn):
    """员工确认后才真正执行：草案建成、凭证消费、pending 置 confirmed。"""
    action_id = _run_to_pending(conn, "s5")
    result = harness.confirm_pending_action(
        conn, action_id=action_id, session_id="s5", employee_no="emp001", cfg={},
    )
    assert result["ok"] and result["biz_no"].startswith("PREP-")
    draft = agent_store.get_draft(conn, result["draft_id"])
    assert draft["kg"] == 49.0 and draft["status"] == "draft"

    pend = conn.execute(
        "SELECT * FROM agent_pending_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    assert pend["status"] == "confirmed" and pend["resolved_by"] == "emp001"
    args = json.loads(pend["arguments_json"])
    assert agent_store.get_token(conn, args["run_id"])["consumed_at"] is not None
    # 确认执行也留轮次日志（归属原轮次，供阶段 5 归因）
    logs = agent_store.list_round_logs(conn, "s5")
    confirm_rows = [r for r in logs if r["kind"] == "tool"
                    and r["tool_name"] == "create_suggestion_draft"]
    assert confirm_rows and confirm_rows[-1]["ok"] == 1


def test_confirm_twice_conflicts(conn):
    action_id = _run_to_pending(conn, "s6")
    harness.confirm_pending_action(
        conn, action_id=action_id, session_id="s6", employee_no="emp001", cfg={},
    )
    with pytest.raises(Exception) as exc:
        harness.confirm_pending_action(
            conn, action_id=action_id, session_id="s6", employee_no="emp001", cfg={},
        )
    assert getattr(exc.value, "code", "") == "ACTION_STATE_CONFLICT"


def test_reject_creates_nothing(conn):
    action_id = _run_to_pending(conn, "s7")
    harness.reject_pending_action(
        conn, action_id=action_id, session_id="s7", employee_no="emp001",
    )
    assert conn.execute("SELECT COUNT(*) FROM suggestion_drafts").fetchone()[0] == 0
    pend = conn.execute(
        "SELECT status FROM agent_pending_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    assert pend["status"] == "rejected"


def test_confirm_wrong_session_rejected(conn):
    """凭证不能跨会话确认——按不存在处理，不泄露 action 是否存在。"""
    action_id = _run_to_pending(conn, "s8")
    with pytest.raises(Exception) as exc:
        harness.confirm_pending_action(
            conn, action_id=action_id, session_id="other-session",
            employee_no="emp001", cfg={},
        )
    assert getattr(exc.value, "code", "") == "ACTION_NOT_FOUND"


# ── orchestrator 兼容壳 ────────────────────────────────────
def test_orchestrator_wrapper_still_works(conn):
    """旧入口（examples/demo、老测试）继续可用：收集事件成 AgentTurn。"""

    def chat(messages, cfg, tools):
        if not any(m.get("role") == "tool" for m in messages):
            return {"content": None, "tool_calls": [{
                "id": "a", "name": "get_sync_status", "arguments": {}}]}
        return {"content": "均为最新。", "tool_calls": []}

    turn = orchestrator.run(
        conn=conn, session_id="s9", employee_no="emp001",
        question="数据新鲜吗", cfg={}, chat_fn=chat,
    )
    assert turn.stop_reason == "answered"
    assert turn.tool_calls == 1
    assert turn.traces[0]["name"] == "get_sync_status"
