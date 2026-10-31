"""智能体 API 集成测试：/api/agent/ask 改为 SSE + 建单确认/拒绝端点。

LLM 打桩（monkeypatch gateway.chat_with_tools），验证：
- SSE 事件序：meta → round* → tool* → draft_preview → done
- 轮次日志与待确认动作真实落库
- confirm / reject 端点的会话归属与幂等约束
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from prep_assistant.agents.llm import gateway
from prep_assistant.api.app import create_app
from prep_assistant.infra import db as database


def _draft_flow_chat(messages, cfg, tools):
    """三步脚本：校验 → 效期 → 建草案（与 harness 测试同一脚本）。"""
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
    return {"content": "", "tool_calls": [{
        "id": "c", "name": "create_suggestion_draft",
        "arguments": {"run_id": run["run_id"], "risk_id": risk["risk_id"],
                      "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"]}}]}


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        event, data = None, None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if event:
            events.append((event, data))
    return events


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """临时库 + 打桩 LLM（脚本化三步建单流程）。"""
    from prep_assistant.agents.tools import tokens

    monkeypatch.setattr(tokens, "_SECRET_PATH", tmp_path / ".agent_secret")
    monkeypatch.setattr(tokens, "_secret_cache", None)
    monkeypatch.setattr(gateway, "chat_with_tools", _draft_flow_chat)

    with TestClient(create_app(db_path=str(tmp_path / "agent.db"))) as test_client:
        yield test_client


def _ask(client, question="备 300 份鸡胸肉，用批次 B20261030-01"):
    resp = client.post("/api/agent/ask", json={"question": question})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(resp.text)


def test_ask_sse_event_order_and_pending(client):
    events = _ask(client)
    names = [e for e, _ in events]
    assert names[0] == "meta"
    assert "draft_preview" in names
    assert names[-1] == "done"

    meta = events[0][1]
    preview = dict(events)["draft_preview"]
    done = dict(events)["done"]
    assert preview["kg"] == 49.0
    assert done["stop_reason"] == "awaiting_confirmation"
    assert done["pending_action_id"] == preview["action_id"]

    # 助手消息落库（确定性模板，不是模型文本）
    conn = database.connect(client.app.state.db_path)
    row = conn.execute(
        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'assistant'"
        " ORDER BY id DESC LIMIT 1", (meta["session_id"],),
    ).fetchone()
    conn.close()
    assert "待确认" in row["content"] and "49.0" in row["content"]

    # 轮次日志已落库
    logs = database.connect(client.app.state.db_path).execute(
        "SELECT kind, tool_name FROM agent_round_logs WHERE session_id = ?",
        (meta["session_id"],),
    ).fetchall()
    kinds = {r["kind"] for r in logs}
    assert {"model", "tool", "interrupt"} <= kinds


def test_confirm_endpoint_creates_draft(client):
    events = _ask(client)
    meta = events[0][1]
    preview = dict(events)["draft_preview"]

    resp = client.post(
        f"/api/agent/draft/{preview['action_id']}/confirm", json={"session_id": meta["session_id"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["biz_no"].startswith("PREP-")

    conn = database.connect(client.app.state.db_path)
    draft = conn.execute(
        "SELECT status, kg FROM suggestion_drafts WHERE draft_id = ?", (body["draft_id"],)
    ).fetchone()
    conn.close()
    assert draft["status"] == "draft" and draft["kg"] == 49.0


def test_reject_endpoint_and_double_confirm_conflict(client):
    events = _ask(client)
    meta = events[0][1]
    preview = dict(events)["draft_preview"]

    resp = client.post(
        f"/api/agent/draft/{preview['action_id']}/confirm", json={"session_id": meta["session_id"]},
    )
    assert resp.status_code == 200
    # 再确认一次 → 409 冲突（不是 500）
    resp2 = client.post(
        f"/api/agent/draft/{preview['action_id']}/confirm", json={"session_id": meta["session_id"]},
    )
    assert resp2.status_code == 409
    assert resp2.json()["code"] == "ACTION_STATE_CONFLICT"


def test_confirm_wrong_session_is_404(client):
    events = _ask(client)
    preview = dict(events)["draft_preview"]
    resp = client.post(
        f"/api/agent/draft/{preview['action_id']}/confirm",
        json={"session_id": "not-your-session"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "ACTION_NOT_FOUND"


def test_reject_flow(client):
    events = _ask(client)
    meta = events[0][1]
    preview = dict(events)["draft_preview"]
    resp = client.post(
        f"/api/agent/draft/{preview['action_id']}/reject", json={"session_id": meta["session_id"]},
    )
    assert resp.status_code == 200
    conn = database.connect(client.app.state.db_path)
    count = conn.execute("SELECT COUNT(*) FROM suggestion_drafts").fetchone()[0]
    conn.close()
    assert count == 0


def test_invalid_question_is_json_error(client):
    resp = client.post("/api/agent/ask", json={"question": ""})
    assert resp.status_code == 422
    assert resp.json()["code"] == "INVALID_QUESTION"
