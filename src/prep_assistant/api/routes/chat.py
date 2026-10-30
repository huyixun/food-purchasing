"""员工问答路由：POST /api/chat（SSE 流式）与历史恢复。"""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Iterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from prep_assistant.agents.llm.gateway import (
    LLMConfigError,
    LLMTimeoutError,
    stream_llm,
)
from prep_assistant.agents.llm.prompts import build_messages, resolve_system_prompt
from prep_assistant.agents.moderation import hits_to_json, scan_pair
from prep_assistant.agents.rag.indexer import build_dense_index, build_or_load_index
from prep_assistant.agents.rag.retriever import hybrid_search
from prep_assistant.api.errors import AppError, sse_event
from prep_assistant.infra.config import REPO_ROOT, load_config
from prep_assistant.infra.db import (
    append_message,
    conversation_exists,
    create_conversation,
    get_history,
    list_knowledge_sources,
    recent_messages,
    upsert_rule_hits,
)

router = APIRouter()

MAX_QUESTION_LEN = 500


class ChatRequest(BaseModel):
    """请求体：session_id 可选（不传则新建），question 必填。"""

    session_id: str | None = None
    question: str = Field(default="")


class RateLimiter:
    """滑动窗口限流（决策：同一会话 60 秒内最多 10 次）。

    内存实现，仅对单进程有效；阶段 3 多 worker 时迁 Redis。
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


def _employee_no(request: Request) -> str:
    """决策：读 X-Employee-No 请求头，缺省 demo；阶段 3 接登录后替换此处。"""
    return (request.headers.get("x-employee-no") or "demo").strip() or "demo"


def _validate_question(raw: str) -> str:
    question = (raw or "").strip()
    if not question or len(question) > MAX_QUESTION_LEN:
        raise AppError("INVALID_QUESTION")
    return question


@router.post("/api/chat")
def post_chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """员工问答入口。

    错误分两段处理（这是本接口最容易做错的地方）：
    - 校验类错误在进入流式**之前**抛 AppError → 返回 JSON 错误体
    - 进入流式之后（检索/生成阶段）的错误 → 发 event: error 后关闭流
    """
    cfg = load_config()
    question = _validate_question(body.question)
    conn = request.app.state.db_conn()

    # 会话：存在性校验优先于限流，避免把不存在的会话也计入频次
    if body.session_id:
        if not conversation_exists(conn, body.session_id):
            raise AppError("SESSION_NOT_FOUND")
        session_id = body.session_id
    else:
        session_id = create_conversation(conn, _employee_no(request))

    limiter: RateLimiter = request.app.state.rate_limiter
    if not limiter.allow(session_id):
        raise AppError("RATE_LIMITED")

    # 先取历史再落当前问题，避免当前问题在历史里重复出现
    history = recent_messages(conn, session_id, cfg.get("chat", {}).get("history_rounds", 7))
    append_message(conn, session_id, "user", question)

    return StreamingResponse(
        event_stream(cfg, conn, request, question, history, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def event_stream(
    cfg: dict, conn, request: Request, question: str, history: list[dict], session_id: str
) -> Iterator[str]:
    """SSE 事件流。闭包变量改为显式入参：生成器定义在模块级，便于测试与复用。"""
    yield sse_event("meta", {"session_id": session_id})

    # ── 检索阶段 ──────────────────────────────────────────
    yield sse_event("status", {"stage": "retrieving", "text": "正在检索制度…"})
    try:
        # 知识来源 = 内置样例 + 管理后台上传的全部来源；
        # 后台增删过知识则强制重建（index_dirty 标记由管理路由维护）
        extra = [
            (s["filename"], REPO_ROOT / s["stored_path"])
            for s in list_knowledge_sources(conn)
        ]
        force = bool(getattr(request.app.state, "index_dirty", False))
        chunks, vectors, labels = build_or_load_index(cfg, force=force, extra_sources=extra)
        request.app.state.index_dirty = False
        dense_index = build_dense_index(vectors)
        hits = hybrid_search(question, chunks, vectors, dense_index, cfg, labels=labels)
    except Exception:  # noqa: BLE001 检索失败不泄漏内部细节
        yield sse_event("error", {"code": "INTERNAL_ERROR"})
        return

    citations = {
        "items": [
            {
                "no": i + 1,
                "source": h["source"],  # 命中片段的来源（内置样例 / 上传文件名）
                "snippet": h["text"][:80],
                "score": h["score"],
            }
            for i, h in enumerate(hits)
        ]
    }
    yield sse_event("citations", citations)
    yield sse_event("status", {"stage": "generating", "text": "正在生成回答…"})

    # ── 生成阶段（真流式，供应商不支持时网关自动降级为伪流式） ──
    # 注：LLM 等待期间暂无法插心跳，阶段 3 换异步 httpx 后补 : ping
    # 系统提示词每次提问都从库里取当前生效版本——管理员改完立即生效
    system_prompt, prompt_version = resolve_system_prompt(conn)
    messages = build_messages(question, hits, history, system_prompt=system_prompt)
    started = time.monotonic()
    parts: list[str] = []
    try:
        for delta in stream_llm(messages, cfg):
            parts.append(delta)
            yield sse_event("delta", {"content": delta})
    except LLMTimeoutError as exc:
        yield sse_event("error", {"code": "LLM_TIMEOUT", "message": str(exc)})
        return
    except LLMConfigError as exc:
        yield sse_event("error", {"code": "LLM_UNAVAILABLE", "message": str(exc)})
        return

    latency_ms = int((time.monotonic() - started) * 1000)
    answer_text = "".join(parts)
    assistant_id = append_message(
        conn,
        session_id,
        "assistant",
        answer_text if answer_text else "（供应商未返回内容）",
        citations=json.dumps(citations["items"], ensure_ascii=False),
        provider=cfg["llm"]["chat_model"],
        prompt_version=prompt_version,
        latency_ms=latency_ms,
    )
    # 违规初筛（确定性规则，决策 B4）：结果落 message_flags，审计页可查。
    # 干净也存空数组，表示「已扫描」，与「未扫描」区分开
    try:
        upsert_rule_hits(conn, assistant_id, hits_to_json(scan_pair(question, answer_text)))
    except Exception:  # noqa: BLE001 初筛失败不影响问答主流程
        pass
    yield sse_event("done", {"assistant_msg_id": assistant_id, "latency_ms": latency_ms})


@router.get("/api/chat/{session_id}/history")
def get_chat_history(session_id: str, request: Request) -> dict:
    """历史恢复：刷新页面后前端据此重绘整个会话。"""
    conn = request.app.state.db_conn()
    if not conversation_exists(conn, session_id):
        raise AppError("SESSION_NOT_FOUND")
    return {"session_id": session_id, "messages": get_history(conn, session_id)}
