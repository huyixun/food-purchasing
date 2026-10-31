"""SQLite 存储层。

表分两组：
- 会话组：conversations / messages（阶段 2 建立）
- 管理后台组：users / prompt_templates / knowledge_sources / message_flags（本阶段建立）

阶段 3 迁 PostgreSQL + SQLAlchemy 时：
- 全部表结构原样平移，语义不变
- 本模块的函数签名作为仓储接口保留，调用方不需要改
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,              -- 即 session_id（UUID）
    employee_no TEXT NOT NULL DEFAULT 'demo',  -- 具名身份；本期读请求头，缺省 demo
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    citations       TEXT,                      -- JSON 数组，仅 assistant 消息有
    provider        TEXT,                      -- 生成该回答的模型，便于归因
    prompt_version  TEXT,                      -- 提示词版本，见 agents/llm/prompts.py
    latency_ms      INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

-- ── 管理后台组（本阶段新增）────────────────────────────────
-- 用户：员工/管理员两种角色；密码只存 PBKDF2 哈希（见 infra/security.py）
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_no   TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('employee', 'admin')),
    created_at    TEXT NOT NULL
);

-- 提示词模板：只增不改（对齐决策记录「字典版本只增不改」的同款纪律）；
-- is_active 同一时刻至多一行 =1，问答路由每次提问都读当前生效版本
CREATE TABLE IF NOT EXISTS prompt_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    version    TEXT NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 知识来源：管理员上传的 md/pdf（pdf 解析出的文本也落成 md 文件）；
-- FAISS 索引 = 内置样例 + 本表全部条目 的合集切片
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,             -- 展示名（含扩展名）
    stored_path   TEXT NOT NULL,             -- 解析后的 md 文件（相对仓库根）
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    embed_channel TEXT NOT NULL,             -- api / local，记录入库时的嵌入通道
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 违规标记：规则初筛结果 + LLM 辅助归因 + 管理员人工认定，三者分开存
CREATE TABLE IF NOT EXISTS message_flags (
    message_id  INTEGER PRIMARY KEY REFERENCES messages(id),
    rule_hits   TEXT,                        -- JSON：[{rule, label, hits}]
    ai_review   TEXT,                        -- LLM 辅助归因文本（仅供参考）
    manual_flag INTEGER NOT NULL DEFAULT 0,  -- 管理员人工认定 1=违规 0=未认定
    updated_at  TEXT NOT NULL
);

-- ── 智能体组（阶段 4 新增，见 docs/agent-tooling-design.md）────────────
-- 批次台账：助手为权威源（决策 B3.2），状态机见 core/batch.py
CREATE TABLE IF NOT EXISTS batches (
    batch_no                 TEXT PRIMARY KEY,
    material_code            TEXT NOT NULL,
    thaw_state               TEXT NOT NULL
        CHECK (thaw_state IN ('待解冻','解冻中','已解冻','加工中','已用完','待报废')),
    remaining_shelf_life_hours REAL,          -- 允许 NULL：缺数据时判定为 block 而非放行
    measured_yield           REAL,            -- 实测出成率，NULL = 走「按基准」
    promo_cancelled          INTEGER NOT NULL DEFAULT 0,
    state_changed_at         TEXT NOT NULL,
    version                  INTEGER NOT NULL DEFAULT 1,  -- 乐观锁，回写时比对
    updated_at               TEXT NOT NULL
);

-- 凭证：T1 签发、T2 消费。四道闸的数据基础（详见 agents/tools/tokens.py）
CREATE TABLE IF NOT EXISTS agent_run_tokens (
    token        TEXT PRIMARY KEY,             -- run_/risk_ + 32 hex
    kind         TEXT NOT NULL CHECK (kind IN ('run','risk')),
    session_id   TEXT NOT NULL,                -- 会话归属，跨会话直接拒
    params_json  TEXT NOT NULL,                -- 签发时的全部事实，用于重建
    params_hash  TEXT NOT NULL,                -- 规范化 JSON 的 SHA-256 前 16 位
    dict_version TEXT,                         -- 计算所用字典版本（f45 追溯）
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT,                         -- 非空 = 已消费（一次性）
    consumed_ref TEXT NOT NULL DEFAULT '',     -- 消费到哪个 draft_id
    hmac         TEXT NOT NULL                 -- 防伪造、防手改库
);

CREATE INDEX IF NOT EXISTS idx_tokens_session ON agent_run_tokens(session_id, kind);

-- 建议草案：T2 产出，状态恒为 draft，须班组长本人确认才生效
CREATE TABLE IF NOT EXISTS suggestion_drafts (
    draft_id      TEXT PRIMARY KEY,
    biz_no        TEXT NOT NULL UNIQUE,
    session_id    TEXT NOT NULL,
    item_code     TEXT NOT NULL,
    portions      INTEGER NOT NULL,
    batch_no      TEXT NOT NULL,
    kg            REAL NOT NULL,
    dict_version  TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    risk_id       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','confirmed','rejected')),
    confirmed_by  TEXT,                        -- 人工确认人工号，模型永不可写
    confirmed_at  TEXT,
    created_at    TEXT NOT NULL
);

-- 交接待办：五要素齐备才建；accepted_at 为空 = 对方尚未确认接手（f23）
CREATE TABLE IF NOT EXISTS handover_tasks (
    task_id        TEXT PRIMARY KEY,
    biz_no         TEXT NOT NULL,
    biz_version    TEXT,
    status_summary TEXT NOT NULL,
    open_items     TEXT NOT NULL,              -- JSON 数组
    from_employee  TEXT NOT NULL,
    to_employee    TEXT NOT NULL,
    due_at         TEXT,
    accepted_at    TEXT,                       -- NULL = 未交接完成
    accepted_by    TEXT,
    created_at     TEXT NOT NULL
);

-- 批次异常上报：只登记与通知，不改批次可用状态（隔离只能质量人员做）
CREATE TABLE IF NOT EXISTS anomaly_reports (
    report_id   TEXT PRIMARY KEY,
    batch_no    TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note        TEXT,
    reported_by TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','handled','closed')),
    created_at  TEXT NOT NULL
);

-- 轮次日志：harness 每一轮模型决策 / 每次工具执行 / 中断与收口都落一条。
-- 用途：阶段 5 的离线分析（哪个工具慢、哪轮失败、改判从哪开始），因此
-- 冗余存 arguments_json 与 result_summary 快照，不做关联查询也能独立复盘。
CREATE TABLE IF NOT EXISTS agent_round_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id        TEXT NOT NULL,            -- 一次 run 的唯一编号
    session_id     TEXT NOT NULL,
    round_no       INTEGER NOT NULL,         -- 1 起始；工具行归属其所在轮
    kind           TEXT NOT NULL
        CHECK (kind IN ('model','tool','interrupt','round_limit')),
    tool_name      TEXT,                     -- kind=model 时为 NULL
    arguments_json TEXT,
    latency_ms     INTEGER,
    ok             INTEGER,                  -- 1 成功 / 0 失败 / NULL 不适用
    error_code     TEXT,
    result_summary TEXT,                     -- 结果摘要（截断），分析时不用回放原文
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_round_logs_session
    ON agent_round_logs(session_id, turn_id, id);

-- 待确认动作：create_suggestion_draft 执行前的强制中断。
-- 确认前草案绝不落业务表；确认/拒绝由员工在本人的会话里点按钮完成。
CREATE TABLE IF NOT EXISTS agent_pending_actions (
    action_id      TEXT PRIMARY KEY,
    turn_id        TEXT NOT NULL,
    session_id     TEXT NOT NULL,            -- 只能由发起会话确认（跨会话按不存在处理）
    employee_no    TEXT NOT NULL,
    tool_name      TEXT NOT NULL,
    arguments_json TEXT NOT NULL,            -- 模型提交的完整参数，确认时原样执行
    round_no       INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','rejected','expired')),
    result_json    TEXT,                     -- 确认后执行结果（draft_id/biz_no…）
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    resolved_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_session ON agent_pending_actions(session_id, status);
"""


def _now() -> str:
    """统一用 UTC ISO 时间；双时态改造（阶段 3）在此处扩展。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接。check_same_thread=False：请求由线程池并发处理，
    本期为单用户原型，写操作都在请求线程内串行完成。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def create_conversation(conn: sqlite3.Connection, employee_no: str) -> str:
    session_id = uuid.uuid4().hex
    now = _now()
    conn.execute(
        "INSERT INTO conversations (id, employee_no, created_at, updated_at) VALUES (?,?,?,?)",
        (session_id, employee_no, now, now),
    )
    conn.commit()
    return session_id


def conversation_exists(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def append_message(
    conn: sqlite3.Connection,
    session_id: str,
    role: str,
    content: str,
    *,
    citations: str | None = None,
    provider: str | None = None,
    prompt_version: str | None = None,
    latency_ms: int | None = None,
) -> int:
    """写入一条消息并刷新会话的 updated_at，返回消息 id。"""
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, citations, provider,"
        " prompt_version, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, role, content, citations, provider, prompt_version, latency_ms, _now()),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), session_id)
    )
    conn.commit()
    return int(cur.lastrowid)


def recent_messages(conn: sqlite3.Connection, session_id: str, rounds: int) -> list[dict]:
    """取最近 rounds 轮（rounds*2 条）历史，按时间正序返回，供拼入提示词。"""
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (session_id, max(rounds, 0) * 2),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def get_history(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """取会话全部消息（历史恢复接口用），按时间正序。"""
    rows = conn.execute(
        "SELECT id, role, content, citations, provider, prompt_version, created_at"
        " FROM messages WHERE conversation_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    result = []
    for r in rows:
        item = {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
            "provider": r["provider"],
            "prompt_version": r["prompt_version"],
            "citations": [],
        }
        if r["citations"]:
            import json

            try:
                item["citations"] = json.loads(r["citations"])
            except ValueError:
                item["citations"] = []  # 脏数据不阻塞历史恢复
        result.append(item)
    return result


# ═══════════════════════ 管理后台：用户 ═══════════════════════

def get_user(conn: sqlite3.Connection, employee_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE employee_no = ?", (employee_no,)
    ).fetchone()


def create_user(
    conn: sqlite3.Connection, employee_no: str, password_hash: str, role: str
) -> None:
    conn.execute(
        "INSERT INTO users (employee_no, password_hash, role, created_at) VALUES (?,?,?,?)",
        (employee_no, password_hash, role, _now()),
    )
    conn.commit()


# ═══════════════════════ 管理后台：提示词模板 ═══════════════════════

def get_active_template(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """当前生效模板；没有（首次启动未播种）返回 None，调用方回退到代码默认值。"""
    return conn.execute(
        "SELECT id, content, version, created_by, created_at FROM prompt_templates"
        " WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()


def list_templates(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, content, version, is_active, created_by, created_at"
        " FROM prompt_templates ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_template(
    conn: sqlite3.Connection, content: str, created_by: str
) -> dict:
    """保存新版本：旧的全部置为失效，新版本成为当前生效。
    返回新版本行（供页面提示版本号）。版本号按历史行数递增（v1、v2、…）。"""
    count = conn.execute("SELECT COUNT(*) FROM prompt_templates").fetchone()[0]
    version = f"v{count + 1}"
    conn.execute("UPDATE prompt_templates SET is_active = 0")
    conn.execute(
        "INSERT INTO prompt_templates (content, version, is_active, created_by, created_at)"
        " VALUES (?,?,1,?,?)",
        (content, version, created_by, _now()),
    )
    conn.commit()
    return {"version": version, "content": content}


# ═══════════════════════ 管理后台：知识来源 ═══════════════════════

def add_knowledge_source(
    conn: sqlite3.Connection,
    filename: str,
    stored_path: str,
    chunk_count: int,
    embed_channel: str,
    created_by: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO knowledge_sources (filename, stored_path, chunk_count,"
        " embed_channel, created_by, created_at) VALUES (?,?,?,?,?,?)",
        (filename, stored_path, chunk_count, embed_channel, created_by, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_knowledge_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, filename, stored_path, chunk_count, embed_channel, created_by,"
        " created_at FROM knowledge_sources ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def count_knowledge_sources(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0])


def get_knowledge_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)
    ).fetchone()


def delete_knowledge_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM knowledge_sources WHERE id = ?", (source_id,))
    conn.commit()


# ═══════════════════════ 管理后台：违规标记 ═══════════════════════

def upsert_rule_hits(conn: sqlite3.Connection, message_id: int, rule_hits: str) -> None:
    """规则初筛结果落库（每条 assistant 消息生成后自动执行一次）。"""
    conn.execute(
        "INSERT INTO message_flags (message_id, rule_hits, manual_flag, updated_at)"
        " VALUES (?,?,0,?)"
        " ON CONFLICT(message_id) DO UPDATE SET rule_hits = excluded.rule_hits,"
        " updated_at = excluded.updated_at",
        (message_id, rule_hits, _now()),
    )
    conn.commit()


def set_ai_review(conn: sqlite3.Connection, message_id: int, review: str) -> None:
    conn.execute(
        "INSERT INTO message_flags (message_id, ai_review, manual_flag, updated_at)"
        " VALUES (?,?,COALESCE((SELECT manual_flag FROM message_flags"
        "  WHERE message_id = ?), 0),?)"
        " ON CONFLICT(message_id) DO UPDATE SET ai_review = excluded.ai_review,"
        " updated_at = excluded.updated_at",
        (message_id, review, message_id, _now()),
    )
    conn.commit()


def set_manual_flag(conn: sqlite3.Connection, message_id: int, flagged: bool) -> None:
    conn.execute(
        "INSERT INTO message_flags (message_id, manual_flag, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(message_id) DO UPDATE SET manual_flag = excluded.manual_flag,"
        " updated_at = excluded.updated_at",
        (message_id, 1 if flagged else 0, _now()),
    )
    conn.commit()


def get_flags(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM message_flags WHERE message_id = ?", (message_id,)
    ).fetchone()


def audit_messages(
    conn: sqlite3.Connection,
    *,
    employee_no: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_flagged: bool = False,
    limit: int = 50,
) -> list[dict]:
    """审计查询：按员工 / 时间段 / 仅看违规 筛选，返回问答对与标记信息。

    时间参数是「本地日期」（YYYY-MM-DD），与 SQLite 里存的 UTC ISO 字符串
    做前缀匹配——原型期精度足够，阶段 3 迁 PG 时改为真正的范围查询。
    """
    sql = (
        "SELECT m.id, m.conversation_id, m.role, m.content, m.citations,"
        " m.created_at, c.employee_no, f.rule_hits, f.ai_review, f.manual_flag"
        " FROM messages m"
        " JOIN conversations c ON c.id = m.conversation_id"
        " LEFT JOIN message_flags f ON f.message_id = m.id"
        " WHERE 1=1"
    )
    params: list = []
    if employee_no:
        sql += " AND c.employee_no = ?"
        params.append(employee_no)
    if date_from:
        sql += " AND m.created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND m.created_at < date(?, '+1 day')"  # 含当天：小于次日
        params.append(date_to)
    if only_flagged:
        sql += " AND (f.manual_flag = 1 OR f.rule_hits IS NOT NULL"
        sql += " AND f.rule_hits != '[]')"
    sql += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        import json

        item = dict(r)
        try:
            item["rule_hits"] = json.loads(r["rule_hits"]) if r["rule_hits"] else []
        except ValueError:
            item["rule_hits"] = []
        item["flagged"] = bool(r["manual_flag"]) or bool(item["rule_hits"])
        result.append(item)
    return result
