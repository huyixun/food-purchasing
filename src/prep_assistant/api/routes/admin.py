"""管理后台：服务端渲染页面 + 对应 API。

页面（Jinja2 SSR，SaaS 风格，统一侧边栏布局）：
    /admin              首页（统计卡片）
    /admin/knowledge    知识库管理（上传 md/pdf / 列表 / 删除）
    /admin/prompts      提示词模板管理（编辑 + 历史版本）
    /admin/audit        对话审计（筛选 / 规则命中标红 / AI 辅助归因 / 人工认定）

API 约定：页面路由未登录 → 302 跳登录页；JSON API 未登录 → 401 错误体。
所有写操作都要求 role=admin（员工角色本期只能用聊天页）。
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from prep_assistant.agents.llm.gateway import call_llm
from prep_assistant.agents.moderation import ai_review_messages
from prep_assistant.api.errors import AppError, error_body
from prep_assistant.infra import db as database
from prep_assistant.infra.config import REPO_ROOT
from prep_assistant.infra.security import require_admin

router = APIRouter()
templates = Jinja2Templates(directory=str(REPO_ROOT / "templates"))

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 上限 5 MB，防误传整库扫描件
ALLOWED_SUFFIXES = {".md", ".markdown", ".pdf"}
UPLOAD_DIR = "data/knowledge"  # 相对仓库根；pdf 解析出的文本也存这里


# ── 页面守卫：未登录跳登录页（页面用重定向，API 用 401） ──────

def _admin_user(request: Request) -> dict:
    """页面守卫：未登录/非管理员 → 重定向登录页（用异常控制流最省事）。"""
    try:
        return require_admin(request)
    except AppError as exc:
        if exc.code == "UNAUTHORIZED":
            raise _RedirectToLogin() from exc
        raise  # FORBIDDEN 照常抛，由页面模板显示 403


class _RedirectToLogin(Exception):
    """内部信号：页面路由捕获后做 302。"""


@router.get("/admin")
def dashboard(request: Request):
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    stats = {
        "sources": database.count_knowledge_sources(conn),
        "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
        "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "flagged": conn.execute(
            "SELECT COUNT(*) FROM message_flags WHERE manual_flag = 1"
            " OR (rule_hits IS NOT NULL AND rule_hits != '[]')"
        ).fetchone()[0],
    }
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "stats": stats}
    )


# ═══════════════════════ 知识库管理 ═══════════════════════

def _extract_text(upload: UploadFile) -> str:
    """按扩展名解析上传文件为纯文本。md 直接读；pdf 用 pypdf 逐页提取。"""
    suffix = Path(upload.filename or "").suffix.lower()
    raw = upload.file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AppError("FILE_TOO_LARGE")
    if suffix == ".pdf":
        from io import BytesIO

        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover
            raise AppError("INTERNAL_ERROR", "服务器未安装 pypdf，请联系管理员") from exc
        try:
            reader = PdfReader(BytesIO(raw))
            text = "\n\n".join(
                (page.extract_text() or "").strip() for page in reader.pages
            ).strip()
        except Exception as exc:  # noqa: BLE001 损坏的 PDF 收敛为中文错误
            raise AppError("FILE_EMPTY", f"PDF 解析失败：文件可能已损坏") from exc
    elif suffix in (".md", ".markdown"):
        text = raw.decode("utf-8", errors="replace").strip()
    else:
        raise AppError("UNSUPPORTED_FILE")
    if not text:
        raise AppError("FILE_EMPTY")
    return text


def _safe_filename(filename: str) -> str:
    """清洗文件名：只留中文/字母/数字/点/下划线/连字符，防路径穿越。"""
    base = Path(filename).name
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", base).strip("._") or "未命名"
    return cleaned[:80]


@router.get("/admin/knowledge")
def knowledge_page(request: Request):
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    return templates.TemplateResponse(
        request,
        "knowledge.html",
        {"user": user, "sources": database.list_knowledge_sources(conn), "error": None},
    )


@router.post("/admin/knowledge/upload")
def knowledge_upload(request: Request, file: UploadFile):
    """上传 md/pdf → 解析 → 切片计数 → 落盘 → 元数据入库 → 索引标记失效。"""
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    try:
        text = _extract_text(file)
        filename = _safe_filename(file.filename or "未命名.md")
        # pdf 存提取出的文本并统一后缀为 .md（切片器只认 markdown）
        if filename.lower().endswith(".pdf"):
            filename = filename[:-4] + ".md"
        stored_rel = f"{UPLOAD_DIR}/{int(time.time())}-{filename}"
        stored_path = REPO_ROOT / stored_rel
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        stored_path.write_text(text, encoding="utf-8")

        # 切片计数：与检索管线用同一个切片器，保证数字一致
        from prep_assistant.agents.rag.chunker import chunk_markdown

        cfg = request.app.state.cfg
        chunk_count = len(chunk_markdown(text, cfg["retrieval"]["chunk_size"]))
        embed_channel = "api" if cfg["llm"].get("embedding_model") else "local"

        database.add_knowledge_source(
            conn, filename, stored_rel, chunk_count, embed_channel, user["employee_no"]
        )
        request.app.state.index_dirty = True  # 下次问答强制重建索引
    except AppError as exc:
        return templates.TemplateResponse(
            request,
            "knowledge.html",
            {
                "user": user,
                "sources": database.list_knowledge_sources(conn),
                "error": exc.message,
            },
            status_code=exc.status,
        )
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/admin/knowledge/{source_id}/delete")
def knowledge_delete(request: Request, source_id: int):
    """删除知识来源：删文件 + 元数据；最后一个来源拒绝删除（问答不能开天窗）。"""
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    row = database.get_knowledge_source(conn, source_id)
    if row is None:
        raise AppError("INVALID_REQUEST", "知识来源不存在")
    allow_delete_last = bool(request.app.state.cfg.get("admin", {}).get("allow_delete_last_source", False))
    if database.count_knowledge_sources(conn) <= 1 and not allow_delete_last:
        raise AppError("LAST_SOURCE")
    stored = REPO_ROOT / row["stored_path"]
    if stored.exists():
        stored.unlink()  # 只删我们落盘的文件，绝不碰源路径之外的任何东西
    database.delete_knowledge_source(conn, source_id)
    request.app.state.index_dirty = True
    return RedirectResponse("/admin/knowledge", status_code=303)


# ═══════════════════════ 提示词模板管理 ═══════════════════════

@router.get("/admin/prompts")
def prompts_page(request: Request):
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    from prep_assistant.agents.llm.prompts import DEFAULT_SYSTEM_PROMPT

    active = database.get_active_template(conn)
    return templates.TemplateResponse(
        request,
        "prompts.html",
        {
            "user": user,
            "active": active,
            "default_prompt": DEFAULT_SYSTEM_PROMPT,
            "history": database.list_templates(conn),
            "saved_version": request.query_params.get("saved"),
        },
    )


@router.post("/admin/prompts/save")
def prompts_save(request: Request, content: str = Form(...)):
    """保存新版本：立即生效（问答路由每次提问都从库里读），旧版本只增不删。"""
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    text = content.strip()
    if not text:
        raise AppError("INVALID_REQUEST", "提示词内容不能为空")
    conn = request.app.state.db_conn()
    saved = database.save_template(conn, text, user["employee_no"])
    return RedirectResponse(f"/admin/prompts?saved={saved['version']}", status_code=303)


# ═══════════════════════ 对话审计 ═══════════════════════

@router.get("/admin/audit")
def audit_page(request: Request):
    try:
        user = _admin_user(request)
    except _RedirectToLogin:
        return RedirectResponse("/admin/login", status_code=302)
    conn = request.app.state.db_conn()
    q = request.query_params
    rows = database.audit_messages(
        conn,
        employee_no=(q.get("employee_no") or "").strip() or None,
        date_from=(q.get("date_from") or "").strip() or None,
        date_to=(q.get("date_to") or "").strip() or None,
        only_flagged=q.get("only_flagged") == "1",
        limit=100,
    )
    # 把每行的会话内相邻问题带上，页面展示「问答对」而不是孤立回答
    enriched = []
    for r in rows:
        item = dict(r)
        if r["role"] == "assistant":
            prev = conn.execute(
                "SELECT content FROM messages WHERE conversation_id = ? AND id < ?"
                " AND role = 'user' ORDER BY id DESC LIMIT 1",
                (r["conversation_id"], r["id"]),
            ).fetchone()
            item["question"] = prev["content"] if prev else ""
        else:
            item["question"] = r["content"]
        enriched.append(item)
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "user": user,
            "rows": enriched,
            "filters": {
                "employee_no": q.get("employee_no") or "",
                "date_from": q.get("date_from") or "",
                "date_to": q.get("date_to") or "",
                "only_flagged": q.get("only_flagged") == "1",
            },
        },
    )


@router.post("/admin/audit/{message_id}/flag")
async def audit_flag(request: Request, message_id: int):
    """管理员人工认定/取消违规（JSON API，审计页 fetch 调用）。"""
    try:
        require_admin(request)
    except AppError as exc:
        return JSONResponse(status_code=exc.status, content=error_body(exc.code))
    body = await request.json()
    conn = request.app.state.db_conn()
    if conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone() is None:
        return JSONResponse(status_code=404, content=error_body("INVALID_REQUEST", "消息不存在"))
    database.set_manual_flag(conn, message_id, bool(body.get("flagged")))
    return {"ok": True, "message_id": message_id, "flagged": bool(body.get("flagged"))}


@router.post("/admin/audit/{message_id}/ai-review")
async def audit_ai_review(request: Request, message_id: int):
    """LLM 辅助归因（管理员手动触发）。

    注意：模型只产出「归因说明」，最终认定永远归管理员；
    响应与落库都带「AI 辅助，仅供参考」标注。
    """
    try:
        require_admin(request)
    except AppError as exc:
        return JSONResponse(status_code=exc.status, content=error_body(exc.code))
    conn = request.app.state.db_conn()
    msg = conn.execute(
        "SELECT m.id, m.role, m.content, m.conversation_id FROM messages m WHERE m.id = ?",
        (message_id,),
    ).fetchone()
    if msg is None:
        return JSONResponse(status_code=404, content=error_body("INVALID_REQUEST", "消息不存在"))
    # 组问答对：审计对象以 assistant 回答为主，问题取同会话相邻的 user 消息
    if msg["role"] == "assistant":
        prev = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND id < ?"
            " AND role = 'user' ORDER BY id DESC LIMIT 1",
            (msg["conversation_id"], msg["id"]),
        ).fetchone()
        question, answer = (prev["content"] if prev else ""), msg["content"]
    else:
        nxt = conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? AND id > ?"
            " AND role = 'assistant' ORDER BY id LIMIT 1",
            (msg["conversation_id"], msg["id"]),
        ).fetchone()
        question, answer = msg["content"], (nxt["content"] if nxt else "")

    flags = database.get_flags(conn, message_id)
    import json as _json

    rule_hits = _json.loads(flags["rule_hits"]) if flags and flags["rule_hits"] else []

    cfg = request.app.state.cfg
    try:
        review = call_llm(ai_review_messages(question, answer, rule_hits), cfg)
    except Exception as exc:  # noqa: BLE001 LLM 故障收敛为中文错误，不阻塞审计页
        return JSONResponse(status_code=503, content=error_body("LLM_UNAVAILABLE", str(exc)))
    database.set_ai_review(conn, message_id, f"【AI 辅助，仅供参考】{review}")
    return {"ok": True, "review": f"【AI 辅助，仅供参考】{review}"}
