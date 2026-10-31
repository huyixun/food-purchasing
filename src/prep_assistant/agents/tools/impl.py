"""8 个工具的实现。原则：工具是薄壳，业务语义全在 core/ 里。

三条边界：
- 只读工具不写任何表；
- 校验工具不写业务表，只签发凭证（凭证表是基础设施，不是业务单据）；
- 草案工具只写自有台账，且状态恒为 draft —— 生效必须等人类点确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from prep_assistant.agents.rag.indexer import build_dense_index, build_or_load_index
from prep_assistant.agents.rag.retriever import hybrid_search
from prep_assistant.agents.tools import tokens
from prep_assistant.agents.tools.errors import ToolError
from prep_assistant.core import shelf_life as shelf_life_core
from prep_assistant.core.conversion import convert
from prep_assistant.core.masterdata import load_dict
from prep_assistant.infra import agent_store, db as database
from prep_assistant.infra.config import REPO_ROOT


@dataclass
class ToolContext:
    """一次对话的工具执行上下文。conn 由请求层持有，工具不自己开关连接。"""

    conn: object
    session_id: str
    employee_no: str = "demo"
    cfg: dict = field(default_factory=dict)
    _index: tuple | None = None          # RAG 索引缓存：同一轮对话只建一次
    _dict: object | None = None

    @property
    def dictionary(self):
        if self._dict is None:
            self._dict = load_dict()
        return self._dict

    def index(self):
        """惰性构建/复用索引（与问答路由同一套多来源逻辑）。"""
        if self._index is None:
            extra = [
                (s["filename"], REPO_ROOT / s["stored_path"])
                for s in database.list_knowledge_sources(self.conn)
            ]
            chunks, vectors, labels = build_or_load_index(
                self.cfg, extra_sources=extra
            )
            self._index = (chunks, vectors, build_dense_index(vectors), labels)
        return self._index


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


# ── T0 只读 ────────────────────────────────────────────────
def search_knowledge(ctx: ToolContext, args: dict) -> dict:
    query = str(args.get("query", "")).strip()
    if len(query) < 2:
        raise ToolError("MISSING_FIELDS", "检索词太短", recover_hint="请给出至少 2 个字的关键词。")
    top_k = int(args.get("top_k", 3))

    chunks, vectors, dense_index, labels = ctx.index()
    cfg = dict(ctx.cfg)
    cfg["retrieval"] = {**cfg.get("retrieval", {}), "top_k": top_k}
    hits = hybrid_search(query, chunks, vectors, dense_index, cfg, labels=labels)
    return {
        "ok": True,
        "items": [
            {"no": i + 1, "source": h["source"], "snippet": h["text"][:120],
             "score": h["score"]}
            for i, h in enumerate(hits)
        ],
        "note": "引用时按 [资料n] 标注，不得改写片段内容。",
    }


def get_batch_status(ctx: ToolContext, args: dict) -> dict:
    batch_no = str(args.get("batch_no", "")).strip()
    if not batch_no:
        raise ToolError("MISSING_FIELDS", "缺少批次号", recover_hint="请向用户确认批次号后再查询。")

    row = agent_store.get_batch(ctx.conn, batch_no)
    if row is None:
        raise ToolError(
            "NOT_FOUND",
            f"批次 {batch_no} 不存在于批次台账",
            recover_hint="不得编造批次。请让用户核对批次号，或先扫码录入该批次。",
        )

    missing = [
        label for key, label in (
            ("remaining_shelf_life_hours", "剩余效期"),
            ("measured_yield", "实测出成率"),
        ) if row.get(key) is None
    ]
    return {
        "ok": True,
        "batch_no": row["batch_no"],
        "material_code": row["material_code"],
        "thaw_state": row["thaw_state"],
        "remaining_shelf_life_hours": row["remaining_shelf_life_hours"],
        "measured_yield": row["measured_yield"],
        "promo_cancelled": bool(row["promo_cancelled"]),
        "state_changed_at": row["state_changed_at"],
        "missing": missing,
        "stale": False,
        "note": "字段为 null 表示台账中确实没有该数据，不得用经验值代替。",
    }


def get_sync_status(ctx: ToolContext, args: dict) -> dict:
    refresh_min = int(ctx.cfg.get("data", {}).get("refresh_interval_minutes", 15))
    threshold = _now() - timedelta(minutes=refresh_min)

    latest = ctx.conn.execute(
        "SELECT MAX(created_at) AS t FROM knowledge_sources"
    ).fetchone()["t"]
    knowledge_at = latest or ""
    stale = True
    if knowledge_at:
        stale = datetime.fromisoformat(knowledge_at.replace("Z", "+00:00")) < threshold

    latest_batch = ctx.conn.execute("SELECT MAX(updated_at) AS t FROM batches").fetchone()["t"]
    prompt = database.get_active_template(ctx.conn)

    return {
        "ok": True,
        "sources": [
            {"name": "知识库", "last_synced_at": knowledge_at or None, "stale": stale},
            {"name": "批次台账", "last_synced_at": latest_batch, "stale": False},
        ],
        "dict_version": ctx.dictionary.version,
        "prompt_version": prompt["version"] if prompt else "default",
        "not_connected": ["ERP", "门店订货平台", "生产台账"],
        "note": "未接入的外部系统数据不可用；外部数据类问题必须说明这一点。",
    }


# ── T1 校验（签发凭证）──────────────────────────────────────
def validate_material_need(ctx: ToolContext, args: dict) -> dict:
    item_code = str(args.get("item_code", "")).strip()
    batch_no = str(args.get("batch_no", "")).strip()
    portions = args.get("portions")

    material = ctx.dictionary.get(item_code)
    if material is None:
        return {
            "ok": False,
            "missing": ["item_code"],
            "missing_labels": ["该品类未在字典中映射"],
            "recover_hint": (
                f"品类 {item_code} 未映射，不能出建议（宁可空白，不可瞎算）。"
                "请向用户确认品类，或提示管理员在字典中补录映射。"
            ),
        }

    batch = agent_store.get_batch(ctx.conn, batch_no) if batch_no else None
    if batch is None and batch_no:
        return {
            "ok": False,
            "missing": ["batch_no"],
            "missing_labels": ["批次台账中查不到该批次"],
            "recover_hint": "请核对批次号，或先在备料工位扫码录入该批次的状态与出成率。",
        }

    result = convert(
        material=material,
        portions=int(portions) if isinstance(portions, int) else None,
        batch_no=batch_no or None,
        measured_yield=batch["measured_yield"] if batch else None,
    )

    if not result.ok:
        return {
            "ok": False,
            "missing": result.missing,
            "missing_labels": result.missing_labels,
            "recover_hint": (
                "缺少的要素不能替你补。请逐项向用户确认；"
                "若该批次无实测出成率，可说明「若按字典基准值计算」需用户明确同意。"
            ),
            "detail": result.detail,
        }

    facts = {
        "item_code": item_code,
        "item_name": material.name,
        "portions": result.detail["portions"],
        "batch_no": batch_no,
        "denominator_g": material.denominator_g,
        "yield_rate": result.detail["yield_rate"],
        "correction_factor": result.detail["correction_factor"],
        "kg": result.kg,
        "dict_version": ctx.dictionary.version,
    }
    token = tokens.issue(
        ctx.conn, kind="run", session_id=ctx.session_id,
        params=facts, dict_version=ctx.dictionary.version,
    )

    return {
        "ok": True,
        "run_id": token["token"],
        "expires_at": token["expires_at"],
        "dict_version": ctx.dictionary.version,
        "suggestion": {
            "item_code": item_code,
            "item_name": material.name,
            "portions": facts["portions"],
            "denominator_g": material.denominator_g,
            "yield_rate": facts["yield_rate"],
            "correction_factor": facts["correction_factor"],
            "kg": result.kg,
            "rounding": "向上取整到 0.1kg",
        },
        "needs_review": result.detail["needs_review"],
        "basis": result.basis,
        "note": (
            "kg 为系统计算值，你必须逐字引用，不得改写或再做任何运算。"
            "建草案还需 check_shelf_life 签发的 risk_id。"
        ),
    }


def check_shelf_life(ctx: ToolContext, args: dict) -> dict:
    batch_no = str(args.get("batch_no", "")).strip()
    planned = str(args.get("planned_start_at", "")).strip()

    batch = agent_store.get_batch(ctx.conn, batch_no)
    if batch is None:
        raise ToolError(
            "NOT_FOUND", f"批次 {batch_no} 不存在",
            recover_hint="请核对批次号；不得为不存在的批次判定效期。",
        )

    material = ctx.dictionary.get(batch["material_code"])
    process_hours = material.process_hours if material else 2.0

    result = shelf_life_core.check(
        remaining_hours=batch["remaining_shelf_life_hours"],
        process_hours=process_hours,
        buffer_hours=ctx.dictionary.buffer_hours,
        warn_ratio=ctx.dictionary.warn_ratio,
        thaw_state=batch["thaw_state"],
        promo_cancelled=bool(batch["promo_cancelled"]),
    )

    facts = {
        "batch_no": batch_no,
        "level": result.level,
        "reason_codes": result.reason_codes,
        "remaining_hours": result.remaining_hours,
        "required_hours": result.required_hours,
        "planned_start_at": planned,
    }
    token = tokens.issue(
        ctx.conn, kind="risk", session_id=ctx.session_id,
        params=facts, dict_version=ctx.dictionary.version,
    )

    return {
        "ok": True,
        "risk_id": token["token"],
        "level": result.level,
        "remaining_hours": result.remaining_hours,
        "required_hours": result.required_hours,
        "process_hours": result.process_hours,
        "buffer_hours": result.buffer_hours,
        "reason_codes": result.reason_codes,
        "reason_labels": result.reason_labels,
        "evidence": result.evidence,
        "advisory": result.advisory,
        "expires_at": token["expires_at"],
        "note": (
            "level=block 时不得建草案，也不得建议延长效期；只能换批次或转质量人员。"
            if result.level == "block" else
            "你无权更改效期、无权放行食品、无权隔离批次。"
        ),
    }


# ── T2 草案（写自有台账）───────────────────────────────────
def create_suggestion_draft(ctx: ToolContext, args: dict) -> dict:
    run_id = str(args.get("run_id", "")).strip()
    risk_id = str(args.get("risk_id", "")).strip()
    item_code = str(args.get("item_code", "")).strip()
    kg = args.get("kg")

    if not isinstance(kg, (int, float)):
        raise ToolError(
            "MISSING_FIELDS", "缺少 kg 或类型不对",
            recover_hint="kg 必须取自 validate_material_need 的返回值，不得自行构造。",
        )

    # 闸门：先校验 run，再校验 risk，最后原子消费
    run_params = tokens.verify_run(
        ctx.conn, run_id=run_id, session_id=ctx.session_id, item_code=item_code, kg=float(kg)
    )
    tokens.verify_risk(ctx.conn, risk_id=risk_id, session_id=ctx.session_id)

    draft_id = agent_store.new_id("drf")
    biz_no = agent_store.next_biz_no(ctx.conn)
    agent_store.insert_draft(ctx.conn, {
        "draft_id": draft_id,
        "biz_no": biz_no,
        "session_id": ctx.session_id,
        "item_code": item_code,
        "portions": run_params["portions"],
        "batch_no": run_params["batch_no"],
        "kg": float(kg),
        "dict_version": run_params["dict_version"],
        "run_id": run_id,
        "risk_id": risk_id,
    })
    tokens.mark_consumed(ctx.conn, run_id, risk_id, draft_id)

    return {
        "ok": True,
        "draft_id": draft_id,
        "biz_no": biz_no,
        "status": "draft",
        "kg": float(kg),
        "confirm_url": f"/prep/drafts/{draft_id}",
        "note": "草案已创建。需班组长本人在界面点击「确认备料」后生效，你不能代其确认。",
    }


def create_handover_task(ctx: ToolContext, args: dict) -> dict:
    required = {
        "biz_no": "业务编号",
        "status_summary": "状态说明",
        "open_items": "未决事项",
        "to_employee_no": "接手人",
    }
    missing = [label for key, label in required.items() if not args.get(key)]
    if missing:
        raise ToolError(
            "MISSING_FIELDS", "交接待办五要素不全",
            missing=missing,
            recover_hint=(
                "交接必须包含：业务编号 / 版本（可选）/ 状态 / 未决事项 / 接手人。"
                "请逐项向用户确认，不得留空占位。"
            ),
        )

    task_id = agent_store.new_id("hov")
    agent_store.insert_handover(ctx.conn, {
        "task_id": task_id,
        "biz_no": args["biz_no"],
        "biz_version": args.get("biz_version"),
        "status_summary": args["status_summary"],
        "open_items": list(args["open_items"]),
        "from_employee": ctx.employee_no,
        "to_employee": args["to_employee_no"],
        "due_at": args.get("due_at"),
    })
    return {
        "ok": True,
        "task_id": task_id,
        "status": "pending_accept",
        "accept_url": f"/prep/handovers/{task_id}",
        "note": "发消息不算交接完成。对方确认接手后才算完成，你不能代替对方确认。",
    }


def report_batch_anomaly(ctx: ToolContext, args: dict) -> dict:
    batch_no = str(args.get("batch_no", "")).strip()
    reason = str(args.get("reason_code", "")).strip()
    if not batch_no or not reason:
        raise ToolError("MISSING_FIELDS", "缺少批次号或异常原因")

    batch = agent_store.get_batch(ctx.conn, batch_no)
    if batch is None:
        raise ToolError("NOT_FOUND", f"批次 {batch_no} 不存在",
                        recover_hint="请核对批次号，不得为不存在的批次上报异常。")

    report_id = agent_store.new_id("anm")
    agent_store.insert_anomaly(ctx.conn, {
        "report_id": report_id,
        "batch_no": batch_no,
        "reason_code": reason,
        "note": args.get("note"),
        "reported_by": ctx.employee_no,
    })
    return {
        "ok": True,
        "report_id": report_id,
        "status": "open",
        "batch_state_unchanged": True,
        "current_thaw_state": batch["thaw_state"],
        "note": "已登记并通知质量人员。本操作不改变批次可用状态；隔离与放行只能由质量人员执行。",
    }


HANDLERS = {
    "search_knowledge": search_knowledge,
    "get_batch_status": get_batch_status,
    "get_sync_status": get_sync_status,
    "validate_material_need": validate_material_need,
    "check_shelf_life": check_shelf_life,
    "create_suggestion_draft": create_suggestion_draft,
    "create_handover_task": create_handover_task,
    "report_batch_anomaly": report_batch_anomaly,
}


def execute(name: str, args: dict, ctx: ToolContext) -> dict:
    """统一入口：错误一律转成结构化 dict 回给模型，不抛栈。

    抛栈给模型只会得到一句"工具出错了"，模型下一步就会编；
    给结构化的 recover_hint，它才能合法地回到正轨。
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolError("TOOL_UNKNOWN", f"不存在名为 {name} 的工具").to_dict()
    try:
        return handler(ctx, args or {})
    except ToolError as exc:
        return exc.to_dict()
    except (KeyError, TypeError, ValueError) as exc:
        return ToolError(
            "MISSING_FIELDS", f"参数不完整或类型错误：{exc}",
            recover_hint="请按工具定义补齐必填参数后重试。",
        ).to_dict()
    except Exception as exc:  # noqa: BLE001 兜底不泄漏内部细节
        return {"ok": False, "code": "INTERNAL_TOOL_ERROR",
                "message": f"工具 {name} 执行失败：{type(exc).__name__}",
                "recover_hint": "请如实告知用户系统异常并转人工处理，不得自行推测结果。"}
