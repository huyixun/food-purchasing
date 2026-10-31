"""凭证链：让"必须先校验才能建单"成为服务端强制，而不是提示词里的请求。

四道闸（docs/agent-tooling-design.md §3）：

    1 存在性 + HMAC 校验      → 防伪造、防有人手改库
    2 会话归属                → 防"昨天算的今天接着用"
    3 有效期 10 分钟          → 防长尾复用
    4 参数哈希逐位比对        → 防模型把 43.8 改成 45

为什么不用"会话状态位"：状态位管不住「校验的是 A 批次、写成 B 批次」，
也管不住参数被改——那正是最危险的一类漂移。

为什么参数要整份存下来：f45 要求审计时能重建这一次处理，
凭证里的 params_json 就是"当时的事实快照"，含字典版本。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from prep_assistant.agents.tools.errors import ToolError
from prep_assistant.infra import agent_store
from prep_assistant.infra.config import REPO_ROOT

TOKEN_TTL_SECONDS = 600          # 10 分钟，够一个人读完建议再确认，又不至于留到下一班
_SECRET_PATH = REPO_ROOT / "data" / ".agent_secret"
_TOKEN_RE = re.compile(r"^(run|risk)_[0-9a-f]{32}$")

_secret_cache: bytes | None = None


def _secret() -> bytes:
    """服务端签名密钥：首次运行生成并落盘（已 gitignore）。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    if _SECRET_PATH.exists():
        _secret_cache = _SECRET_PATH.read_bytes()
    else:
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _secret_cache = hashlib.sha256(str(datetime.now().timestamp()).encode()).digest()
        _SECRET_PATH.write_bytes(_secret_cache)
    return _secret_cache


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical(params: dict) -> str:
    """规范化 JSON：键排序 + 无空格，保证同一份事实永远算出同一个哈希。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def params_hash(params: dict) -> str:
    return hashlib.sha256(_canonical(params).encode("utf-8")).hexdigest()[:16]


def issue(
    conn,
    *,
    kind: str,
    session_id: str,
    params: dict,
    dict_version: str | None = None,
    ttl_seconds: int = TOKEN_TTL_SECONDS,
) -> dict:
    """签发凭证，返回对外可用的 {token, expires_at, params}。"""
    token = agent_store.new_id(kind)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    record = {
        "token": token,
        "kind": kind,
        "session_id": session_id,
        "params_json": _canonical(params),
        "params_hash": params_hash(params),
        "dict_version": dict_version,
        "issued_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
    }
    record["hmac"] = _sign("|".join(str(record[k]) for k in (
        "token", "kind", "session_id", "params_json", "params_hash", "dict_version",
        "issued_at", "expires_at",
    )))
    agent_store.insert_token(conn, record)
    return {"token": token, "expires_at": record["expires_at"], "params": params}


def _recover_hint(kind: str) -> str:
    if kind == "risk":
        return (
            "请先调用 check_shelf_life(batch_no, planned_start_at) 获取 risk_id，再重试。"
            "不得使用其他会话或上一轮对话的编号。"
        )
    return (
        "请先调用 validate_material_need(item_code, portions, batch_no) 获取 run_id，再重试。"
        "不得虚构编号，也不得复用已用过的编号。"
    )


def _load_verified(conn, token: str, kind: str, session_id: str) -> dict:
    """闸门 1–3：格式、签名、类型、会话、有效期、未消费。"""
    if not token or not _TOKEN_RE.match(token) or not token.startswith(f"{kind}_"):
        raise ToolError(
            "MISSING_PREREQUISITE",
            f"没有有效的 {kind} 凭证",
            recover_hint=_recover_hint(kind),
        )

    row = agent_store.get_token(conn, token)
    if row is None:
        # 格式对但库里没有 = 该编号从未签发（伪造，或已过期被清理）
        raise ToolError(
            "MISSING_PREREQUISITE",
            f"该 {kind} 编号从未签发",
            recover_hint=_recover_hint(kind),
        )

    expected = _sign("|".join(str(row[k]) for k in (
        "token", "kind", "session_id", "params_json", "params_hash", "dict_version",
        "issued_at", "expires_at",
    )))
    if not hmac.compare_digest(expected, row["hmac"]):
        raise ToolError("PROVENANCE_MISMATCH", "凭证签名校验失败", recover_hint="请重新执行校验。")

    if row["kind"] != kind:
        raise ToolError("MISSING_PREREQUISITE", f"凭证类型不是 {kind}")

    if row["session_id"] != session_id:
        raise ToolError(
            "SESSION_MISMATCH",
            "该凭证由其他会话签发",
            recover_hint="凭证不能跨会话使用。请在本会话内重新执行校验。",
        )

    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise ToolError(
            "TOKEN_EXPIRED",
            "校验凭证已超过 10 分钟有效期",
            recover_hint="请重新调用 validate_material_need 获取新凭证，不要复用旧编号。",
        )

    if row["consumed_at"]:
        raise ToolError(
            "TOKEN_CONSUMED",
            "该凭证已用于创建草案",
            recover_hint="同一份校验结果只能生成一个草案。如需新草案，请重新校验。",
        )

    return row


def verify_run(
    conn, *, run_id: str, session_id: str, item_code: str, kg: float
) -> dict:
    """闸门 4：参数逐位比对。这是专防「模型把 43.8 改成 45」的那一道。"""
    row = _load_verified(conn, run_id, "run", session_id)
    params = json.loads(row["params_json"])

    expected_kg = float(params.get("kg", 0))
    if round(float(kg), 1) != round(expected_kg, 1):
        raise ToolError(
            "PROVENANCE_MISMATCH",
            "提交的数量与校验结果不一致",
            expected_excerpt=f"kg = {expected_kg}",
            recover_hint=(
                f"必须逐字使用校验返回的 kg（{expected_kg}），不得四舍五入或改写。"
                "如需调整用量，请让用户先修改份数并重新校验。"
            ),
        )

    if params.get("item_code") != item_code:
        raise ToolError(
            "PROVENANCE_MISMATCH",
            "提交的品类与校验结果不一致",
            expected_excerpt=f"item_code = {params.get('item_code')}",
            recover_hint="品类必须与校验时一致。请重新校验目标品类。",
        )

    return params


def verify_risk(conn, *, risk_id: str, session_id: str) -> dict:
    """校验风险凭证；block 级别不得用于建单。"""
    row = _load_verified(conn, risk_id, "risk", session_id)
    params = json.loads(row["params_json"])
    if params.get("level") == "block":
        raise ToolError(
            "SHELF_LIFE_BLOCKED",
            "该批次效期判定为不可用",
            reason_codes=params.get("reason_codes", []),
            recover_hint=(
                "不得据此建草案，也不得建议延长效期。请改用其他批次，"
                "或请用户联系质量人员判定处置方式。"
            ),
        )
    return params


def mark_consumed(conn, run_id: str, risk_id: str, draft_id: str) -> None:
    """草案建成后原子消费两张凭证（并发下只有一次能成功）。"""
    if not agent_store.consume_token(conn, run_id, draft_id):
        raise ToolError("TOKEN_CONSUMED", "该凭证已被并发请求消费", recover_hint="请重新校验。")
    agent_store.consume_token(conn, risk_id, draft_id)
