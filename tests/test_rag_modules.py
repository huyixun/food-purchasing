"""agents 层单元测试：切片器、提示词组装、限流器、错误契约。"""

from __future__ import annotations

import pytest

from prep_assistant.agents.llm.prompts import DEFAULT_SYSTEM_PROMPT, build_messages
from prep_assistant.agents.rag.chunker import chunk_markdown
from prep_assistant.api.errors import ERRORS, AppError, error_body, sse_event
from prep_assistant.api.routes.chat import RateLimiter


# ── 切片器：标题边界优先 ──────────────────────────────────────

def test_chunker_keeps_sections_apart() -> None:
    text = "## 甲制度\n\n" + "甲" * 50 + "\n\n## 乙制度\n\n" + "乙" * 50
    chunks = chunk_markdown(text, size=500)
    assert len(chunks) == 2  # 不同标题绝不混进同一段
    assert chunks[0].startswith("## 甲制度")
    assert chunks[1].startswith("## 乙制度")


def test_chunker_hard_splits_overlong_paragraph() -> None:
    chunks = chunk_markdown("字" * 1200, size=500)
    assert len(chunks) == 3  # 500 + 500 + 200
    assert all(len(c) <= 500 for c in chunks)


def test_chunker_merges_small_paragraphs_in_same_section() -> None:
    text = "## 同一主题\n\n第一段。\n\n第二段。\n\n第三段。"
    assert chunk_markdown(text, size=500) == ["## 同一主题\n第一段。\n第二段。\n第三段。"]


# ── 提示词：system → 历史 → 当前问题 ─────────────────────────

def test_build_messages_inserts_history_between_system_and_question() -> None:
    history = [
        {"role": "user", "content": "上一问"},
        {"role": "assistant", "content": "上一答"},
    ]
    msgs = build_messages("这一问", [{"text": "资料内容"}], history)
    assert msgs[0]["role"] == "system"
    assert msgs[1]["content"] == "上一问"
    assert msgs[2]["content"] == "上一答"
    assert msgs[3]["role"] == "user"
    assert "【问题】这一问" in msgs[3]["content"]
    assert "[资料1]" in msgs[3]["content"]
    assert DEFAULT_SYSTEM_PROMPT.startswith("你是公司备料助手")  # 出厂默认模板（备料口径）


# ── 限流器：滑动窗口 ─────────────────────────────────────────

def test_rate_limiter_blocks_after_max() -> None:
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    assert limiter.allow("s1")
    assert limiter.allow("s1")
    assert not limiter.allow("s1")  # 第 3 次被拒
    assert limiter.allow("s2")  # 其他会话不受影响


# ── 错误契约：code + 中文信息，未知 code 收敛 ─────────────────

def test_error_body_defaults_and_fallback() -> None:
    body = error_body("INVALID_QUESTION")
    assert body == {"code": "INVALID_QUESTION", "message": ERRORS["INVALID_QUESTION"][1]}
    leaked = error_body("NOT_A_REAL_CODE")
    assert leaked["code"] == "INTERNAL_ERROR"  # 不向外暴露未知错误码


def test_app_error_carries_status() -> None:
    err = AppError("SESSION_NOT_FOUND")
    assert err.status == 404
    with pytest.raises(ValueError):
        AppError("NOT_A_REAL_CODE")  # 拼错错误码在开发期就该爆出来


def test_sse_event_format() -> None:
    raw = sse_event("delta", {"content": "你好"})
    assert raw.startswith("event: delta\ndata: ")
    assert raw.endswith("\n\n")
