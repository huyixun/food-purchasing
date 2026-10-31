"""兼容壳：旧入口 orchestrator.run 现在委托给 agents/harness.py。

真正的执行循环、轮次日志与建单中断都在 harness 里实现（见其模块注释）。
保留本模块只为不让 examples/agent_demo.py 与既有测试失效；
新代码请直接使用 prep_assistant.agents.harness。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prep_assistant.agents import harness
from prep_assistant.agents.harness import MAX_ROUNDS  # noqa: F401  旧常量名沿用

# 旧测试引用的名字：语义已从「工具调用预算」变为「轮数上限」
MAX_TOOL_CALLS = MAX_ROUNDS


@dataclass
class AgentTurn:
    """一次智能体执行的完整结果，含可审计的调用轨迹。"""

    answer: str
    traces: list[dict] = field(default_factory=list)
    tool_calls: int = 0
    stop_reason: str = "answered"      # answered / no_content / round_limit / awaiting_confirmation
    messages: list[dict] = field(default_factory=list)


def run(*, conn, session_id: str, employee_no: str, question: str, cfg: dict,
        history: list[dict] | None = None, system_prompt: str | None = None,
        chat_fn=None) -> AgentTurn:
    """收集 harness 事件流，还原成旧的 AgentTurn 结构（同步、非流式）。"""
    answer, traces, tool_calls, stop = "", [], 0, "answered"
    for ev in harness.run(
        conn=conn, session_id=session_id, employee_no=employee_no, question=question,
        cfg=cfg, history=history, system_prompt=system_prompt, chat_fn=chat_fn,
    ):
        if ev["event"] == "tool":
            d = ev["data"]
            tool_calls += 1
            traces.append({
                "name": d["name"], "arguments": d.get("arguments"),
                "ok": d["ok"], "error_code": d.get("error_code"),
                "duration_ms": d.get("duration_ms"),
            })
        elif ev["event"] == "final":
            d = ev["data"]
            answer = d.get("answer", "")
            stop = d["stop_reason"]
    return AgentTurn(
        answer=answer, traces=traces, tool_calls=tool_calls,
        stop_reason=stop, messages=[],
    )


def suggest_followup(turn: AgentTurn) -> list[str]:
    """按最后一次工具结果给出可点击的下一步（前端用，不参与模型决策）。"""
    if not turn.traces:
        return []
    last = turn.traces[-1]
    code = last.get("error_code")
    hints = {
        "MISSING_PREREQUISITE": ["告诉班组长先跑换算校验"],
        "PROVENANCE_MISMATCH": ["核对份数后重新校验"],
        "TOKEN_EXPIRED": ["重新校验（凭证已过 10 分钟）"],
        "SHELF_LIFE_BLOCKED": ["查其他批次", "联系质量人员"],
        "NOT_FOUND": ["核对批次号"],
    }
    return hints.get(code, [])


__all__ = ["AgentTurn", "MAX_TOOL_CALLS", "MAX_ROUNDS", "run", "suggest_followup"]
