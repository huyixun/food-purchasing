"""Agent Harness：智能体执行循环（用户裁定的框架形态 = 自研 Harness 模式）。

为什么叫 Harness 而不是继续叫 orchestrator：
    Python 生态没有名为 "Harness" 的智能体框架（Harness 是 CI/CD 产品）；
    业界把「模型决策 → 工具执行 → 结果回填 → 再决策」这套宿主循环统称为
    agent harness。上一轮已冻结「自写薄循环」（决策 D3），本模块就是把这个
    决策正式化成一个可测试的类——而不是引入 LangGraph/AutoGen：
    护栏必须可审计（f45），框架的状态机会把"哪一步被强制校验"藏进抽象里。

新规格（用户裁定，相对 orchestrator 的三点变化）：
    1. 上限从「6 次工具调用」改为「最多 5 轮模型决策」（1 轮 = 模型 1 次决策）；
    2. 每轮决策与每次工具执行都落 agent_round_logs（阶段 5 分析用）；
    3. create_suggestion_draft 执行前强制中断：
       预校验通过 → 挂起 agent_pending_actions → 把草案预览流式发给员工；
       员工在独立接口确认后才真正执行。预校验失败则错误回填模型继续循环——
       模型仍可自愈，但绝不能绕过（真正的闸门在确认接口里再走一遍）。

事件协议（生成器逐个 yield，路由层转 SSE）：
    meta           {session_id, turn_id}
    round          {round_no}
    tool           {round_no, name, arguments, ok, error_code, duration_ms, summary}
    draft_preview  {action_id, round_no, item_code, item_name, portions,
                    batch_no, kg, risk_level, dict_version}
    final          {answer, stop_reason, round_no, tool_calls,
                    pending_action_id?, preview?}
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from prep_assistant.agents.llm import gateway
from prep_assistant.agents.llm.prompts import build_agent_system
from prep_assistant.agents.tools.errors import ToolError
from prep_assistant.agents.tools.impl import ToolContext, execute
from prep_assistant.agents.tools.schemas import TOOL_SCHEMAS
from prep_assistant.agents.tools import tokens
from prep_assistant.api.errors import AppError
from prep_assistant.infra import agent_store

MAX_ROUNDS = 5                    # 模型决策轮数上限（用户裁定）
GUARDED_TOOLS = {"create_suggestion_draft"}   # 执行前必须人工确认的工具
PENDING_TTL_SECONDS = 600         # 待确认动作有效期，与凭证 TTL 对齐（10 分钟）
SUMMARY_CHARS = 300               # 日志里结果摘要的截断长度
MAX_TOOL_RESULT_CHARS = 4000      # 单条工具结果回填上限


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clip_summary(result: dict) -> str:
    """结果摘要：够阶段 5 分析即可，不存全文。"""
    if result.get("ok"):
        brief = {k: result[k] for k in ("run_id", "risk_id", "level", "kg", "draft_id",
                                        "biz_no", "status", "task_id", "report_id") if k in result}
        text = json.dumps(brief, ensure_ascii=False)
    else:
        text = json.dumps({"code": result.get("code"),
                           "message": result.get("message")}, ensure_ascii=False)
    return text[:SUMMARY_CHARS]


def _clip(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + "…（已截断）"


def _precheck_draft(ctx: ToolContext, args: dict) -> tuple[dict | None, dict | None]:
    """建单前的服务端预校验（闸门 1–4，不消费凭证）。

    返回 (preview, error)：preview 非 None 表示可以挂起等确认；
    error 非 None 表示预校验失败，错误要作为工具结果回填给模型。
    为什么预校验：让「改数 / 伪造凭证 / block 批次」在打扰用户**之前**就被拦下，
    模型有机会按 recover_hint 自愈；确认接口执行时还会再过一遍完整闸门。
    """
    kg = args.get("kg")
    run_id = str(args.get("run_id", "")).strip()
    risk_id = str(args.get("risk_id", "")).strip()
    item_code = str(args.get("item_code", "")).strip()

    if not isinstance(kg, (int, float)) or not run_id or not risk_id or not item_code:
        return None, ToolError(
            "MISSING_FIELDS", "建草案缺少必要参数",
            recover_hint="需要 run_id / risk_id / item_code / kg，均取自校验工具的返回值。",
        ).to_dict()
    try:
        run_params = tokens.verify_run(
            ctx.conn, run_id=run_id, session_id=ctx.session_id,
            item_code=item_code, kg=float(kg),
        )
        risk_params = tokens.verify_risk(ctx.conn, risk_id=risk_id, session_id=ctx.session_id)
    except ToolError as exc:
        return None, exc.to_dict()

    preview = {
        "item_code": item_code,
        "item_name": run_params.get("item_name"),
        "portions": run_params.get("portions"),
        "batch_no": run_params.get("batch_no"),
        "kg": round(float(kg), 1),
        "risk_level": risk_params.get("level"),
        "dict_version": run_params.get("dict_version"),
    }
    return preview, None


# ── 执行循环 ────────────────────────────────────────────────
def run(
    *,
    conn,
    session_id: str,
    employee_no: str,
    question: str,
    cfg: dict,
    history: list[dict] | None = None,
    system_prompt: str | None = None,
    chat_fn=None,
):
    """执行一轮智能体对话，逐个 yield 事件 dict。

    chat_fn 可注入（测试/演示用假实现），默认走网关的 chat_with_tools
    （经 gateway 模块间接引用，测试可以 monkeypatch）。
    历史只取对话文本，不重放历史工具调用（凭证一次性，重放必然失败）。
    """
    chat = chat_fn or gateway.chat_with_tools
    ctx = ToolContext(conn=conn, session_id=session_id, employee_no=employee_no, cfg=cfg)
    turn_id = agent_store.new_id("trn")

    yield {"event": "meta", "data": {"session_id": session_id, "turn_id": turn_id}}

    messages: list[dict] = [{"role": "system", "content": build_agent_system(system_prompt)}]
    for item in history or []:
        messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": question})

    tool_calls = 0

    for round_no in range(1, MAX_ROUNDS + 1):
        yield {"event": "round", "data": {"round_no": round_no}}

        started = time.monotonic()
        try:
            resp = chat(messages, cfg, TOOL_SCHEMAS)
        except gateway.LLMTimeoutError:
            agent_store.log_round(
                conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                kind="model", ok=0, error_code="LLM_TIMEOUT", result_summary="生成超时",
            )
            raise
        except gateway.LLMConfigError:
            agent_store.log_round(
                conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                kind="model", ok=0, error_code="LLM_UNAVAILABLE", result_summary="供应商不可用",
            )
            raise
        latency = int((time.monotonic() - started) * 1000)

        calls = resp.get("tool_calls") or []
        answer = (resp.get("content") or "").strip()

        if not calls:
            # 模型不再调工具 → 本轮文本就是最终回答
            agent_store.log_round(
                conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                kind="model", latency_ms=latency, ok=1,
                result_summary=f"给出最终回答（{len(answer)} 字）",
            )
            yield {"event": "final", "data": {
                "answer": answer, "stop_reason": "answered" if answer else "no_content",
                "round_no": round_no, "tool_calls": tool_calls,
            }}
            return

        agent_store.log_round(
            conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
            kind="model", latency_ms=latency, ok=1,
            result_summary=f"请求 {len(calls)} 个工具调用：{', '.join(c['name'] for c in calls)}",
        )

        # 按 OpenAI 协议回填 assistant(tool_calls)，再逐条回填 tool 结果
        messages.append({
            "role": "assistant",
            "content": resp.get("content") or "",
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in calls
            ],
        })

        for call in calls:
            name = call["name"]
            args = call["arguments"] or {}
            started = time.monotonic()

            if name in GUARDED_TOOLS:
                preview, error = _precheck_draft(ctx, args)
                if error is not None:
                    # 预校验失败：当作一次普通工具失败回填，模型可自愈
                    elapsed = int((time.monotonic() - started) * 1000)
                    tool_calls += 1
                    agent_store.log_round(
                        conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                        kind="tool", tool_name=name,
                        arguments_json=_clip(args), latency_ms=elapsed, ok=0,
                        error_code=error.get("code"),
                        result_summary=_clip_summary(error),
                    )
                    yield {"event": "tool", "data": {
                        "round_no": round_no, "name": name, "arguments": args,
                        "ok": False, "error_code": error.get("code"),
                        "duration_ms": elapsed, "summary": error.get("message", ""),
                    }}
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "name": name, "content": _clip(error)})
                    continue

                # 预校验通过：挂起待确认，本轮终止。凭证未消费，
                # 确认接口执行时会再过一遍完整闸门（含 TTL 与原子消费）。
                action_id = agent_store.new_id("act")
                agent_store.insert_pending_action(conn, {
                    "action_id": action_id, "turn_id": turn_id,
                    "session_id": session_id, "employee_no": employee_no,
                    "tool_name": name, "arguments_json": json.dumps(args, ensure_ascii=False),
                    "round_no": round_no,
                })
                agent_store.log_round(
                    conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                    kind="interrupt", tool_name=name,
                    arguments_json=_clip(args), ok=1, error_code=None,
                    result_summary=f"已挂起待员工确认（action={action_id}）",
                )
                preview_data = {**preview, "action_id": action_id, "round_no": round_no}
                yield {"event": "draft_preview", "data": preview_data}
                yield {"event": "final", "data": {
                    "answer": "", "stop_reason": "awaiting_confirmation",
                    "round_no": round_no, "tool_calls": tool_calls,
                    "pending_action_id": action_id, "preview": preview_data,
                }}
                return

            result = execute(name, args, ctx)
            elapsed = int((time.monotonic() - started) * 1000)
            tool_calls += 1
            ok = bool(result.get("ok", False))
            agent_store.log_round(
                conn, turn_id=turn_id, session_id=session_id, round_no=round_no,
                kind="tool", tool_name=name, arguments_json=_clip(args),
                latency_ms=elapsed, ok=1 if ok else 0,
                error_code=result.get("code") if not ok else None,
                result_summary=_clip_summary(result),
            )
            yield {"event": "tool", "data": {
                "round_no": round_no, "name": name, "arguments": args,
                "ok": ok, "error_code": result.get("code") if not ok else None,
                "duration_ms": elapsed, "summary": _clip_summary(result),
            }}
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "name": name, "content": _clip(result)})

    # ── 轮次耗尽：强制收口，不给工具，逼模型用已有信息作答 ──
    agent_store.log_round(
        conn, turn_id=turn_id, session_id=session_id, round_no=MAX_ROUNDS,
        kind="round_limit", ok=0, error_code="ROUND_LIMIT",
        result_summary=f"已达 {MAX_ROUNDS} 轮上限，强制收口",
    )
    messages.append({
        "role": "user",
        "content": (
            f"系统提示：已达到 {MAX_ROUNDS} 轮上限，不能再调用工具。"
            "请基于已获得的信息直接回答，明确说明哪些事项需要人工确认。"
        ),
    })
    final = chat(messages, cfg, [])
    answer = (final.get("content") or "").strip()
    yield {"event": "final", "data": {
        "answer": answer or "本轮处理步骤较多，已中止。请把问题拆小一些，或联系管理员。",
        "stop_reason": "round_limit", "round_no": MAX_ROUNDS, "tool_calls": tool_calls,
    }}


# ── 确认 / 拒绝（员工专属，模型永不可达）────────────────────
def _load_resolvable(conn, action_id: str, session_id: str) -> dict:
    """校验存在性 + 会话归属 + 状态 + 有效期。跨会话按不存在处理（不泄露）。"""
    row = agent_store.get_pending_action(conn, action_id)
    if row is None or row["session_id"] != session_id:
        raise AppError("ACTION_NOT_FOUND")
    if row["status"] != "pending":
        raise AppError("ACTION_STATE_CONFLICT")
    created = datetime.fromisoformat(row["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if _now() > created + timedelta(seconds=PENDING_TTL_SECONDS):
        agent_store.resolve_pending_action(conn, action_id, "expired", resolved_by="system")
        raise AppError("ACTION_EXPIRED")
    return row


def confirm_pending_action(
    conn, *, action_id: str, session_id: str, employee_no: str, cfg: dict
) -> dict:
    """员工确认后才真正执行 create_suggestion_draft。

    执行走 execute() —— 闸门（存在性/HMAC/会话/TTL/参数逐位/原子消费）
    在工具实现里原样再跑一遍，这里不做任何旁路。
    """
    row = _load_resolvable(conn, action_id, session_id)
    args = json.loads(row["arguments_json"])

    ctx = ToolContext(conn=conn, session_id=session_id, employee_no=employee_no, cfg=cfg)
    started = time.monotonic()
    result = execute(row["tool_name"], args, ctx)
    elapsed = int((time.monotonic() - started) * 1000)

    ok = bool(result.get("ok", False))
    agent_store.log_round(
        conn, turn_id=row["turn_id"], session_id=session_id, round_no=row["round_no"],
        kind="tool", tool_name=row["tool_name"],
        arguments_json=row["arguments_json"], latency_ms=elapsed,
        ok=1 if ok else 0, error_code=result.get("code") if not ok else None,
        result_summary=(
            f"员工 {employee_no} 确认后执行：{_clip_summary(result)}"
            if ok else f"员工确认后执行失败：{_clip_summary(result)}"
        ),
    )

    if not ok:
        # 凭证类失败（过期/已消费）没有重试价值，直接作废；其余保持 pending 可重试
        if result.get("code") in ("TOKEN_EXPIRED", "TOKEN_CONSUMED"):
            agent_store.resolve_pending_action(conn, action_id, "expired",
                                               resolved_by=employee_no)
            raise AppError("ACTION_EXPIRED")
        raise AppError("INTERNAL_ERROR", f"确认执行失败：{result.get('message', '未知原因')}")

    agent_store.resolve_pending_action(
        conn, action_id, "confirmed",
        result_json=json.dumps(result, ensure_ascii=False), resolved_by=employee_no,
    )
    return result


def reject_pending_action(
    conn, *, action_id: str, session_id: str, employee_no: str
) -> None:
    """员工拒绝：什么都不执行，凭证留给 TTL 自然过期。"""
    row = _load_resolvable(conn, action_id, session_id)
    agent_store.resolve_pending_action(conn, action_id, "rejected", resolved_by=employee_no)
    agent_store.log_round(
        conn, turn_id=row["turn_id"], session_id=session_id, round_no=row["round_no"],
        kind="tool", tool_name=row["tool_name"],
        arguments_json=row["arguments_json"], ok=0, error_code="REJECTED_BY_EMPLOYEE",
        result_summary=f"员工 {employee_no} 拒绝了草案请求，未创建任何单据",
    )
