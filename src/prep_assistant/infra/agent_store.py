"""智能体组存储（批次台账 / 凭证 / 草案 / 交接 / 异常上报）。

表结构定义在 infra/db.py 的 _SCHEMA 里（单一来源），本模块只提供仓储函数。
阶段 3 迁 PG 时函数签名保留，调用方不改。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    """对外编号：前缀 + 32 位 hex（凭证号与业务号共用规则，便于模型校验格式）。"""
    return f"{prefix}_{uuid.uuid4().hex}"


# ── 批次台账 ────────────────────────────────────────────────
def get_batch(conn: sqlite3.Connection, batch_no: str) -> dict | None:
    row = conn.execute("SELECT * FROM batches WHERE batch_no = ?", (batch_no,)).fetchone()
    return dict(row) if row else None


def upsert_batch(conn: sqlite3.Connection, **fields) -> None:
    """写入或更新批次。状态变更会 version+1，供回写时的乐观锁比对。"""
    now = _now()
    exists = conn.execute(
        "SELECT version FROM batches WHERE batch_no = ?", (fields["batch_no"],)
    ).fetchone()
    if exists:
        conn.execute(
            "UPDATE batches SET material_code=?, thaw_state=?, remaining_shelf_life_hours=?,"
            " measured_yield=?, promo_cancelled=?, state_changed_at=?, version=?, updated_at=?"
            " WHERE batch_no=?",
            (
                fields["material_code"], fields["thaw_state"],
                fields.get("remaining_shelf_life_hours"), fields.get("measured_yield"),
                int(fields.get("promo_cancelled", 0)), now, exists["version"] + 1, now,
                fields["batch_no"],
            ),
        )
    else:
        conn.execute(
            "INSERT INTO batches (batch_no, material_code, thaw_state, remaining_shelf_life_hours,"
            " measured_yield, promo_cancelled, state_changed_at, version, updated_at)"
            " VALUES (?,?,?,?,?,?,?,1,?)",
            (
                fields["batch_no"], fields["material_code"], fields["thaw_state"],
                fields.get("remaining_shelf_life_hours"), fields.get("measured_yield"),
                int(fields.get("promo_cancelled", 0)), now, now,
            ),
        )
    conn.commit()


def seed_batches(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """幂等播种演示批次（仅当表为空时写入，不覆盖真实数据）。"""
    count = conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"]
    if count:
        return 0
    for row in rows:
        upsert_batch(conn, **row)
    return len(rows)


# 演示/测试用的四个批次，刻意覆盖四种判定分支（不用于生产）
DEMO_BATCHES = [
    # 正常：效期充足、有实测出成率
    {"batch_no": "B20261030-01", "material_code": "M-CHK-001", "thaw_state": "已解冻",
     "remaining_shelf_life_hours": 6.5, "measured_yield": 0.92},
    # 缺效期：应被判 block（缺数据 != 安全）
    {"batch_no": "B20261030-02", "material_code": "M-VEG-003", "thaw_state": "解冻中",
     "remaining_shelf_life_hours": None, "measured_yield": 0.86},
    # 缺出成率：应不出建议值
    {"batch_no": "B20261030-03", "material_code": "M-CHK-001", "thaw_state": "已解冻",
     "remaining_shelf_life_hours": 8.0, "measured_yield": None},
    # 促销取消 + 已解冻：应触发场景拦截 warn
    {"batch_no": "B20261030-04", "material_code": "M-CHK-001", "thaw_state": "已解冻",
     "remaining_shelf_life_hours": 2.0, "measured_yield": 0.90, "promo_cancelled": 1},
]


def seed_demo_batches(conn: sqlite3.Connection) -> int:
    return seed_batches(conn, DEMO_BATCHES)


# ── 凭证 ────────────────────────────────────────────────────
def insert_token(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT INTO agent_run_tokens (token, kind, session_id, params_json, params_hash,"
        " dict_version, issued_at, expires_at, hmac) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            record["token"], record["kind"], record["session_id"], record["params_json"],
            record["params_hash"], record.get("dict_version"), record["issued_at"],
            record["expires_at"], record["hmac"],
        ),
    )
    conn.commit()


def get_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM agent_run_tokens WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def consume_token(conn: sqlite3.Connection, token: str, ref: str) -> bool:
    """原子消费：只有 consumed_at 为空时才成功，天然防并发重复建单。"""
    cur = conn.execute(
        "UPDATE agent_run_tokens SET consumed_at = ?, consumed_ref = ?"
        " WHERE token = ? AND consumed_at IS NULL",
        (_now(), ref, token),
    )
    conn.commit()
    return cur.rowcount == 1


# ── 建议草案 ────────────────────────────────────────────────
def next_biz_no(conn: sqlite3.Connection) -> str:
    """业务编号：PREP-YYYYMMDD-####（按当日草案数递增，人工可读）。"""
    day = datetime.now().strftime("%Y%m%d")
    prefix = f"PREP-{day}-"
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM suggestion_drafts WHERE biz_no LIKE ?", (prefix + "%",)
    ).fetchone()
    return f"{prefix}{row['c'] + 1:04d}"


def insert_draft(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT INTO suggestion_drafts (draft_id, biz_no, session_id, item_code, portions,"
        " batch_no, kg, dict_version, run_id, risk_id, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'draft',?)",
        (
            record["draft_id"], record["biz_no"], record["session_id"], record["item_code"],
            record["portions"], record["batch_no"], record["kg"], record["dict_version"],
            record["run_id"], record["risk_id"], _now(),
        ),
    )
    conn.commit()


def get_draft(conn: sqlite3.Connection, draft_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM suggestion_drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    return dict(row) if row else None


def confirm_draft(conn: sqlite3.Connection, draft_id: str, employee_no: str) -> bool:
    """人工确认（只由前端按钮调用，永不经模型）。已确认的不可重复确认。"""
    cur = conn.execute(
        "UPDATE suggestion_drafts SET status='confirmed', confirmed_by=?, confirmed_at=?"
        " WHERE draft_id = ? AND status = 'draft'",
        (employee_no, _now(), draft_id),
    )
    conn.commit()
    return cur.rowcount == 1


# ── 交接待办 ────────────────────────────────────────────────
def insert_handover(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT INTO handover_tasks (task_id, biz_no, biz_version, status_summary, open_items,"
        " from_employee, to_employee, due_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            record["task_id"], record["biz_no"], record.get("biz_version"),
            record["status_summary"], json.dumps(record["open_items"], ensure_ascii=False),
            record["from_employee"], record["to_employee"], record.get("due_at"), _now(),
        ),
    )
    conn.commit()


def accept_handover(conn: sqlite3.Connection, task_id: str, employee_no: str) -> bool:
    """对方确认接手（f23：发消息不算，确认才算）。只由前端按钮调用。"""
    cur = conn.execute(
        "UPDATE handover_tasks SET accepted_at=?, accepted_by=?"
        " WHERE task_id=? AND accepted_at IS NULL",
        (_now(), employee_no, task_id),
    )
    conn.commit()
    return cur.rowcount == 1


# ── 异常上报 ────────────────────────────────────────────────
def insert_anomaly(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT INTO anomaly_reports (report_id, batch_no, reason_code, note, reported_by,"
        " status, created_at) VALUES (?,?,?,?,?,'open',?)",
        (
            record["report_id"], record["batch_no"], record["reason_code"],
            record.get("note"), record["reported_by"], _now(),
        ),
    )
    conn.commit()


# ── 轮次日志（阶段 5 分析的数据底座）───────────────────────
def log_round(conn: sqlite3.Connection, **fields) -> None:
    """写一条轮次日志。字段缺失时存 NULL，不阻塞主流程。"""
    conn.execute(
        "INSERT INTO agent_round_logs (turn_id, session_id, round_no, kind, tool_name,"
        " arguments_json, latency_ms, ok, error_code, result_summary, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            fields["turn_id"], fields["session_id"], fields["round_no"], fields["kind"],
            fields.get("tool_name"), fields.get("arguments_json"), fields.get("latency_ms"),
            fields.get("ok"), fields.get("error_code"), fields.get("result_summary"),
            _now(),
        ),
    )
    conn.commit()


def list_round_logs(conn: sqlite3.Connection, session_id: str, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM agent_round_logs WHERE session_id = ? ORDER BY id LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── 待确认动作（create_suggestion_draft 强制中断的载体）────
def insert_pending_action(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT INTO agent_pending_actions (action_id, turn_id, session_id, employee_no,"
        " tool_name, arguments_json, round_no, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,'pending',?)",
        (
            record["action_id"], record["turn_id"], record["session_id"],
            record["employee_no"], record["tool_name"], record["arguments_json"],
            record["round_no"], _now(),
        ),
    )
    conn.commit()


def get_pending_action(conn: sqlite3.Connection, action_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM agent_pending_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return dict(row) if row else None


def resolve_pending_action(
    conn: sqlite3.Connection, action_id: str, status: str,
    result_json: str | None = None, resolved_by: str = "",
) -> bool:
    """原子解析：只有 pending 状态能被 resolution 改写，天然防并发重复确认。"""
    cur = conn.execute(
        "UPDATE agent_pending_actions SET status=?, result_json=?, resolved_at=?, resolved_by=?"
        " WHERE action_id = ? AND status = 'pending'",
        (status, result_json, _now(), resolved_by, action_id),
    )
    conn.commit()
    return cur.rowcount == 1
