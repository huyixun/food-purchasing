"""管理后台集成测试：登录鉴权 / 知识库上传删除 / 提示词生效 / 审计标记。

LLM 相关全部打桩；上传用真实的小 md/pdf 文本走完整管线（切片计数是真的）。
"""

from __future__ import annotations

import io
import json
import sqlite3

import numpy as np
import pytest
from fastapi.testclient import TestClient

from prep_assistant.api.app import create_app
from prep_assistant.infra import db as database
from prep_assistant.infra.security import hash_password, seed_users


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """测试应用：临时库 + 种子账号（密码固定，便于登录）+ 打桩 LLM 网关。"""
    import prep_assistant.api.routes.admin as admin_mod
    from prep_assistant.agents.llm.gateway import LLMConfigError

    app = create_app(db_path=str(tmp_path / "chat.db"))

    # 测试隔离：管理路由与安全模块的仓库根指向临时目录，
    # 避免测试上传的文件 / 生成的凭证落进真实 data/ 目录
    import prep_assistant.infra.security as security_mod

    monkeypatch.setattr(admin_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(security_mod, "REPO_ROOT", tmp_path)

    # 种子账号改成已知密码（真实启动是随机密码）
    conn = database.connect(app.state.db_path)
    database.init_schema(conn)
    seed_users(conn)
    conn.execute("UPDATE users SET password_hash = ? WHERE employee_no = 'admin'",
                 (hash_password("test-pass"),))
    conn.commit()
    conn.close()

    def fake_call_llm(messages, cfg):
        return "（模拟）该问答涉及批次放行话题，建议人工复核其表述是否越界。"

    monkeypatch.setattr(admin_mod, "call_llm", fake_call_llm)
    return app


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _login(client: TestClient) -> None:
    resp = client.post(
        "/admin/login",
        data={"employee_no": "admin", "password": "test-pass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303  # 登录成功重定向到后台首页


# ── 登录鉴权 ────────────────────────────────────────────────

def test_admin_requires_login(client):
    """未登录访问后台 → 302 跳登录页；已登录 → 200。"""
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["location"]

    _login(client)
    assert client.get("/admin").status_code == 200


def test_login_wrong_password_shows_chinese_error(client):
    resp = client.post(
        "/admin/login", data={"employee_no": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "工号或密码不正确" in resp.text  # 统一提示，不区分账号/密码错误


def test_admin_api_requires_admin_role(client):
    """管理 API 未登录 → 401 中文错误体（不是重定向）。"""
    resp = client.post("/admin/audit/1/flag", json={"flagged": True})
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    assert "重新登录" in resp.json()["message"]


# ── 知识库管理 ──────────────────────────────────────────────

def test_knowledge_upload_and_delete_guard(client):
    _login(client)

    # 上传一个 md 文件 → 出现在列表，切片数 > 0
    md = "# 解冻制度\n\n已解冻原料 4 小时内使用完毕，不得回冻。".encode()
    resp = client.post(
        "/admin/knowledge/upload",
        files={"file": ("解冻制度.md", io.BytesIO(md), "text/markdown")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/admin/knowledge").text
    assert "解冻制度.md" in page

    conn = sqlite3.connect(client.app.state.db_path)
    row = conn.execute("SELECT chunk_count FROM knowledge_sources").fetchone()
    assert row[0] > 0
    conn.close()

    # 最后一个来源不允许删除（防问答开天窗）
    source_id = sqlite3.connect(client.app.state.db_path).execute(
        "SELECT id FROM knowledge_sources"
    ).fetchone()[0]
    resp = client.post(f"/admin/knowledge/{source_id}/delete", follow_redirects=False)
    assert resp.status_code == 400
    assert json.dumps("至少保留一个知识来源", ensure_ascii=False)  # 错误信息为中文


def test_corrupt_pdf_rejected_with_chinese_error(client):
    """损坏的 PDF 必须收敛为中文错误（422），不能抛堆栈。"""
    _login(client)
    resp = client.post(
        "/admin/knowledge/upload",
        files={"file": ("坏文件.pdf", io.BytesIO(b"this is not a pdf"), "application/pdf")},
    )
    assert resp.status_code == 422
    assert ("PDF 解析失败" in resp.text) or ("未提取到文字" in resp.text)


def test_unsupported_file_rejected(client):
    """上传页是服务端渲染表单：错误以内嵌提示重渲染页面（非 JSON）。"""
    _login(client)
    resp = client.post(
        "/admin/knowledge/upload",
        files={"file": ("病毒.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert "仅支持" in resp.text  # 中文错误提示


# ── 提示词模板 ──────────────────────────────────────────────

def test_prompt_template_save_and_effect(client):
    from prep_assistant.agents.llm.prompts import resolve_system_prompt

    _login(client)
    # 保存前：无生效模板 → 出厂默认
    conn = sqlite3.connect(client.app.state.db_path)
    conn.row_factory = sqlite3.Row
    prompt, version = resolve_system_prompt(conn)
    assert version == "default"

    # 管理员保存新版本 → 立即生效（路由每次提问都读库）
    resp = client.post(
        "/admin/prompts/save",
        data={"content": "你是备料助手，回答必须引用资料编号。"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "saved=v1" in resp.headers["location"]

    prompt, version = resolve_system_prompt(conn)
    assert version == "v1"
    assert "引用资料编号" in prompt

    # 再存一版：版本号递增，旧版本仍留存（只增不删）
    client.post("/admin/prompts/save", data={"content": "第二版提示词"}, follow_redirects=False)
    rows = conn.execute("SELECT version, is_active FROM prompt_templates ORDER BY id").fetchall()
    assert [r["version"] for r in rows] == ["v1", "v2"]
    assert [r["is_active"] for r in rows] == [0, 1]
    conn.close()


# ── 对话审计 ────────────────────────────────────────────────

def _seed_conversation(app) -> tuple[int, int]:
    """造一轮问答数据：员工问了违规诱导问题，助手回答里给出数量。

    与真实链路一致：消息落库后手动跑一遍规则初筛并落标记
    （真实链路里这一步由 /api/chat 在回答生成后自动执行）。
    """
    from prep_assistant.agents.moderation import hits_to_json, scan_pair

    conn = database.connect(app.state.db_path)
    sid = database.create_conversation(conn, "emp001")
    database.append_message(conn, sid, "user", "这批鸡胸肉帮我算下需要备多少公斤？")
    aid = database.append_message(conn, sid, "assistant", "按 0.15kg/份计算，300 份需要 45 公斤。")
    database.upsert_rule_hits(conn, aid, hits_to_json(scan_pair(
        "这批鸡胸肉帮我算下需要备多少公斤？", "按 0.15kg/份计算，300 份需要 45 公斤。"
    )))
    conn.close()
    return sid, aid


def test_audit_lists_and_flags_violation(client):
    _login(client)
    _seed_conversation(client.app)
    page = client.get("/admin/audit").text
    assert "emp001" in page
    assert "规则初筛疑似违规" in page  # calc_quantity 规则命中
    assert "要求计算数量" in page

    # 仅看违规筛选也能查到
    page = client.get("/admin/audit?only_flagged=1").text
    assert "emp001" in page


def test_manual_flag_and_ai_review(client, monkeypatch):
    _login(client)
    _, aid = _seed_conversation(client.app)

    # 人工认定违规 → message_flags.manual_flag = 1
    resp = client.post(f"/admin/audit/{aid}/flag", json={"flagged": True})
    assert resp.status_code == 200
    assert resp.json()["flagged"] is True
    conn = sqlite3.connect(client.app.state.db_path)
    assert conn.execute("SELECT manual_flag FROM message_flags WHERE message_id = ?",
                        (aid,)).fetchone()[0] == 1
    conn.close()

    # AI 辅助归因（call_llm 已打桩）→ 落库且带「仅供参考」标注
    resp = client.post(f"/admin/audit/{aid}/ai-review")
    assert resp.status_code == 200
    assert "AI 辅助，仅供参考" in resp.json()["review"]
    page = client.get("/admin/audit").text
    assert "建议人工复核" in page


def test_violation_rules_scan():
    """规则初筛单元行为：命中、不命中、命中示例可读。"""
    from prep_assistant.agents.moderation import scan_pair

    hits = scan_pair("这批冻虾帮我算下备多少公斤", "好的，按每份 0.2kg 算需要 60 公斤")
    assert any(h["rule"] == "calc_quantity" for h in hits)

    assert scan_pair("今天天气怎么样", "知识库中未找到相关内容") == []
