"""智能体入口（阶段 4b）：POST /api/agent/ask（SSE 流式）+ 草案确认/拒绝。

与 /api/chat 的分工：
- /api/chat       固定流程问答：检索 → 生成，不调工具
- /api/agent/ask  自主决策：模型选工具 → harness 执行 → 回填 → 再决策，
                  事件逐条转 SSE；create_suggestion_draft 触发强制中断，
                  前端收到 draft_preview 后渲染确认卡片，员工点确认/拒绝
                  走本路由的 confirm / reject 端点，模型不参与该环节。

SSE 事件序：meta → round* → tool* → draft_preview? → answer* → done
流内错误发 event: error 后关闭（错误码与 /api/chat 同一套契约）。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from prep_assistant.agents import harness
from prep_assistant.agents.llm.gateway import LLMConfigError, LLMTimeoutError
from prep_assistant.agents.llm.prompts import agent_version, resolve_system_prompt
from prep_assistant.api.errors import AppError, sse_event
from prep_assistant.api.routes.chat import RateLimiter, _employee_no, _validate_question
from prep_assistant.infra.config import load_config
from prep_assistant.infra.db import (
    append_message,
    conversation_exists,
    create_conversation,
    recent_messages,
)

router = APIRouter(tags=["agent"])

ANSWER_CHUNK = 24      # 最终回答伪流式：每片字符数（工具循环本身必须非流式）


class AgentAskRequest(BaseModel):
    session_id: str | None = None
    question: str = Field(default="")


class ConfirmRequest(BaseModel):
    session_id: str


@router.post("/api/agent/ask")
def post_agent_ask(body: AgentAskRequest, request: Request):
    """进入流式前的校验失败 → 直接 JSON 错误（与 /api/chat 同契约）。"""
    cfg = load_config()
    question = _validate_question(body.question)
    employee_no = _employee_no(request)
    conn = request.app.state.db_conn()
    limiter: RateLimiter = request.app.state.rate_limiter

    try:
        if body.session_id:
            if not conversation_exists(conn, body.session_id):
                raise AppError("SESSION_NOT_FOUND")
            session_id = body.session_id
        else:
            session_id = create_conversation(conn, employee_no)

        if not limiter.allow(session_id):
            raise AppError("RATE_LIMITED")

        # 先取历史再落当前问题，避免当前问题在上下文里出现两次
        rounds = cfg.get("chat", {}).get("history_rounds", 7)
        history = recent_messages(conn, session_id, rounds)
        append_message(conn, session_id, "user", question)

        system_prompt, prompt_version = resolve_system_prompt(conn)
    finally:
        conn.close()

    def event_stream():
        answer_text = ""
        stop_reason = "answered"
        pending_id = None
        preview = None
        round_no = 0
        tool_calls = 0

        try:
            conn = request.app.state.db_conn()
            try:
                for ev in harness.run(
                    conn=conn,
                    session_id=session_id,
                    employee_no=employee_no,
                    question=question,
                    cfg=cfg,
                    history=history,
                    system_prompt=system_prompt,
                ):
                    name, data = ev["event"], ev["data"]
                    if name == "final":
                        answer_text = data.get("answer", "")
                        stop_reason = data["stop_reason"]
                        pending_id = data.get("pending_action_id")
                        preview = data.get("preview")
                        round_no = data.get("round_no", 0)
                        tool_calls = data.get("tool_calls", 0)
                    elif name == "draft_preview":
                        preview = data
                        yield sse_event("draft_preview", data)
                    else:
                        yield sse_event(name, data)
            finally:
                conn.close()
        except LLMTimeoutError as exc:
            yield sse_event("error", {"code": "LLM_TIMEOUT", "message": str(exc)})
            return
        except LLMConfigError as exc:
            yield sse_event("error", {"code": "LLM_UNAVAILABLE", "message": str(exc)})
            return

        # ── 落库助手消息：中断场景用确定性模板，不给模型再开口的机会 ──
        if stop_reason == "awaiting_confirmation" and preview:
            text = (
                f"已生成备料建议草案（待确认）：{preview.get('item_name', preview['item_code'])} "
                f"{preview['portions']} 份 → {preview['kg']} kg，"
                f"批次 {preview['batch_no']}，效期判定 {preview['risk_level']}。"
                "请点击确认或取消；确认后仍需班组长本人在界面生效。"
            )
        else:
            text = answer_text or "（模型未返回内容）"

        try:
            conn = request.app.state.db_conn()
            message_id = append_message(
                conn, session_id, "assistant", text,
                provider=cfg["llm"]["chat_model"],
                prompt_version=agent_version(prompt_version),
            )
        finally:
            conn.close()

        # 最终回答伪流式推送（工具循环必须拿到完整 arguments，不能真流式）
        if stop_reason != "awaiting_confirmation":
            for i in range(0, len(text), ANSWER_CHUNK):
                yield sse_event("answer", {"content": text[i:i + ANSWER_CHUNK]})

        yield sse_event("done", {
            "session_id": session_id,
            "assistant_msg_id": message_id,
            "stop_reason": stop_reason,
            "round_no": round_no,
            "tool_calls": tool_calls,
            "pending_action_id": pending_id,
            "prompt_version": agent_version(prompt_version),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/agent/draft/{action_id}/confirm")
def post_confirm(action_id: str, body: ConfirmRequest, request: Request) -> dict:
    """员工确认 → 真正执行 create_suggestion_draft（完整闸门在工具实现里）。"""
    cfg = load_config()
    employee_no = _employee_no(request)
    conn = request.app.state.db_conn()
    try:
        result = harness.confirm_pending_action(
            conn, action_id=action_id, session_id=body.session_id,
            employee_no=employee_no, cfg=cfg,
        )
        # 确认后的回执消息落会话（确定性模板，B4：不让模型转述关键状态）
        append_message(
            conn, body.session_id, "assistant",
            f"草案 {result['biz_no']} 已创建：{result['kg']} kg，状态「待班组长本人确认」。"
            f"确认入口：{result['confirm_url']}",
            provider="system", prompt_version="system-confirm",
        )
    finally:
        conn.close()
    return result


@router.post("/api/agent/draft/{action_id}/reject")
def post_reject(action_id: str, body: ConfirmRequest, request: Request) -> dict:
    """员工拒绝：不执行、不留草案，凭证留待 TTL 自然过期。"""
    employee_no = _employee_no(request)
    conn = request.app.state.db_conn()
    try:
        harness.reject_pending_action(
            conn, action_id=action_id, session_id=body.session_id,
            employee_no=employee_no,
        )
        append_message(
            conn, body.session_id, "assistant",
            "已按你的操作取消该草案请求，未创建任何单据。",
            provider="system", prompt_version="system-confirm",
        )
    finally:
        conn.close()
    return {"ok": True, "status": "rejected"}
