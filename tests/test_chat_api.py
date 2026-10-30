"""员工问答 API 集成测试：LLM 与检索全部打桩，验证 SSE 事件序与落库。"""

from __future__ import annotations

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from prep_assistant.api.app import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """构建测试应用：临时 SQLite + 打桩检索与 LLM，不触网、不花钱。"""
    import prep_assistant.api.routes.chat as chat_mod

    app = create_app(db_path=str(tmp_path / "chat.db"))

    def fake_build_index(cfg, force=False, extra_sources=None):
        return (
            ["片段甲：交接制度内容", "片段乙：效期规则内容"],
            np.zeros((2, 8), dtype="float32"),
            ["内置样例", "内置样例"],
        )

    def fake_search(question, chunks, vectors, index, cfg, labels=None):
        return [
            {"text": chunks[0], "source": labels[0] if labels else "内置样例",
             "dense": 0.5, "bm25": 1.0, "score": 0.75},
            {"text": chunks[1], "source": labels[1] if labels and len(labels) > 1 else "内置样例",
             "dense": 0.1, "bm25": 0.0, "score": 0.05},
        ]

    def fake_stream(messages, cfg):
        yield "你好"
        yield "，世界"

    monkeypatch.setattr(chat_mod, "build_or_load_index", fake_build_index)
    monkeypatch.setattr(chat_mod, "build_dense_index", lambda vectors: None)
    monkeypatch.setattr(chat_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(chat_mod, "stream_llm", fake_stream)

    with TestClient(app) as test_client:
        yield test_client


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 (event, data) 列表，供断言事件顺序。"""
    events = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        event, data = None, None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if event is not None:
            events.append((event, json.loads(data) if data else {}))
    return events


def test_index_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "备料助手" in resp.text  # 聊天页可访问


def test_invalid_question_returns_chinese_error(client):
    for bad in ("", "   ", "字" * 501):
        resp = client.post("/api/chat", json={"question": bad})
        assert resp.status_code == 422
        assert resp.json()["code"] == "INVALID_QUESTION"
        assert "500 字" in resp.json()["message"]  # 中文错误信息


def test_unknown_session_returns_404(client):
    resp = client.post("/api/chat", json={"session_id": "no-such", "question": "hi"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


def test_chat_full_flow_event_order_and_persistence(client):
    resp = client.post("/api/chat", json={"question": "交接时对方不确认怎么办？"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    names = [name for name, _ in events]
    # 事件序：meta 开头 → 状态 → 引用 → 增量 → done 结尾
    assert names[0] == "meta"
    assert "status" in names and "citations" in names and "done" in names
    assert names[-1] == "done"

    by_name = {name: data for name, data in events if name != "status"}
    session_id = by_name["meta"]["session_id"]
    deltas = [data["content"] for name, data in events if name == "delta"]
    assert "".join(deltas) == "你好，世界"  # 打字机增量可完整拼接
    assert by_name["citations"]["items"][0]["source"] == "内置样例"
    assert by_name["citations"]["items"][0]["no"] == 1

    # 落库：user + assistant 各一条，assistant 带引用与提示词版本
    history = client.get(f"/api/chat/{session_id}/history").json()
    roles = [m["role"] for m in history["messages"]]
    assert roles == ["user", "assistant"]
    assert history["messages"][1]["content"] == "你好，世界"
    assert history["messages"][1]["citations"][0]["snippet"].startswith("片段甲")
    assert history["messages"][1]["prompt_version"] == "default"  # 库中无管理员模板时回退出厂默认


def test_history_of_unknown_session_is_404(client):
    resp = client.get("/api/chat/no-such/history")
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


def test_session_reuse_keeps_history(client):
    first = client.post("/api/chat", json={"question": "第一问"}).text
    session_id = json.loads(first.split("\n\n")[0].split("data:")[1])["session_id"]
    client.post("/api/chat", json={"session_id": session_id, "question": "第二问"})
    history = client.get(f"/api/chat/{session_id}/history").json()
    assert len(history["messages"]) == 4  # 两轮各 user+assistant
