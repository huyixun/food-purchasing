"""智能体工具层演示：不需要 API 密钥，直接看凭证链怎么拦住"跳过校验"。

跑法：
    PYTHONPATH=src python examples/agent_demo.py

分两段：
    第一段  逐条撞击四道闸，打印每次的判定与错误码（证明服务端真的拦得住）
    第二段  用假的"模型"跑完整编排循环，看正常链路如何一步不落地走完
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prep_assistant.agents import orchestrator
from prep_assistant.agents.tools import ToolContext, execute
from prep_assistant.infra import agent_store, db as database
from prep_assistant.infra.config import load_config

DEMO_DB = Path(__file__).resolve().parents[1] / "data" / "tmp_agent_demo.db"
SESSION = "sess-demo-0001"


def _show(step: str, result: dict) -> None:
    """只打印判定结论，不打印整坨 JSON。"""
    if result.get("ok"):
        brief = {k: result[k] for k in ("kg", "level", "run_id", "risk_id", "draft_id", "biz_no")
                 if k in result}
        flag = "阻断" if result.get("level") == "block" else "通过"
        print(f"  [{flag}] {step} → {brief}")
    else:
        print(f"  [拒绝] {step} → {result.get('code')}：{result.get('message')}")
        if result.get("expected_excerpt"):
            print(f"          凭证里的值：{result['expected_excerpt']}")
        if result.get("recover_hint"):
            print(f"          回正轨：{result['recover_hint']}")


def part1_gates(conn, cfg) -> None:
    ctx = ToolContext(conn=conn, session_id=SESSION, employee_no="emp001", cfg=cfg)
    print("\n=== 第一段：逐条撞击四道闸 ===")

    print("\n① 模型伪造凭证直接建单（跳过校验）")
    _show("create_suggestion_draft(run_id=run_123)", execute("create_suggestion_draft", {
        "run_id": "run_123", "risk_id": "risk_456", "item_code": "M-CHK-001", "kg": 45,
    }, ctx))

    print("\n② 正常校验：300 份鸡胸肉 / 批次 B20261030-01")
    run = execute("validate_material_need", {
        "item_code": "M-CHK-001", "portions": 300, "batch_no": "B20261030-01",
    }, ctx)
    _show("validate_material_need", run)
    print(f"          口径：{run.get('basis')}")
    run_id = run["run_id"]
    real_kg = run["suggestion"]["kg"]

    print(f"\n③ 模型把 {real_kg} 改成 45 再建单（四道闸的第 4 道）")
    _show("create_suggestion_draft(kg=45)", execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": "risk_" + "0" * 32, "item_code": "M-CHK-001", "kg": 45,
    }, ctx))

    print("\n④ 数字对了，但没有 risk_id（效期没查）")
    _show("create_suggestion_draft(缺 risk)", execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": "risk_" + "0" * 32, "item_code": "M-CHK-001", "kg": real_kg,
    }, ctx))

    print("\n⑤ 跨会话复用他人凭证")
    other = ToolContext(conn=conn, session_id="sess-other", employee_no="emp002", cfg=cfg)
    _show("create_suggestion_draft(其他会话)", execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": "risk_" + "0" * 32, "item_code": "M-CHK-001", "kg": real_kg,
    }, other))

    print("\n⑥ 查一个缺效期的批次：缺数据不当安全")
    risk_block = execute("check_shelf_life", {
        "batch_no": "B20261030-02", "planned_start_at": "2026-10-31T07:00:00+08:00",
    }, ctx)
    _show("check_shelf_life(缺效期)", risk_block)
    print(f"          证据：{risk_block.get('evidence')}")
    print(f"          建议：{risk_block.get('advisory')}")

    print("\n⑦ 拿 block 级凭证去建单")
    _show("create_suggestion_draft(block)", execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": risk_block["risk_id"],
        "item_code": "M-CHK-001", "kg": real_kg,
    }, ctx))

    print("\n⑧ 补齐正确链路：先查效期，再建草案")
    risk_ok = execute("check_shelf_life", {
        "batch_no": "B20261030-01", "planned_start_at": "2026-10-31T07:00:00+08:00",
    }, ctx)
    _show("check_shelf_life", risk_ok)
    draft = execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": risk_ok["risk_id"], "item_code": "M-CHK-001", "kg": real_kg,
    }, ctx)
    _show("create_suggestion_draft", draft)
    print(f"          提示：{draft.get('note')}")

    print("\n⑨ 同一份校验结果再建一次（凭证一次性）")
    _show("create_suggestion_draft(复用)", execute("create_suggestion_draft", {
        "run_id": run_id, "risk_id": risk_ok["risk_id"], "item_code": "M-CHK-001", "kg": real_kg,
    }, ctx))

    print("\n⑩ 人类专属动作不在工具清单里")
    for hidden in ("confirm_suggestion", "accept_handover", "request_writeback", "isolate_batch"):
        _show(hidden, execute(hidden, {}, ctx))


class FakePlanner:
    """脚本化的假模型：按"已有几条工具结果"决定下一步调什么。

    真实模型由 LLM 决定顺序，这里只是把正确路径演一遍，
    用来验证编排循环、消息回填、trace 记录是否都对。
    """

    def __init__(self) -> None:
        self.tool_results: list[dict] = []

    def __call__(self, messages, cfg, tools):
        results = [
            json.loads(m["content"]) for m in messages
            if m.get("role") == "tool" and not m["content"].endswith("（结果过长已截断）")
        ]
        # 从最近的成功结果里取凭证
        run = next((r for r in reversed(results) if r.get("run_id")), None)
        risk = next((r for r in reversed(results) if r.get("risk_id")), None)

        if run is None:
            return {"content": None, "tool_calls": [{
                "id": "c1", "name": "validate_material_need",
                "arguments": {"item_code": "M-CHK-001", "portions": 300,
                              "batch_no": "B20261030-01"},
            }]}
        if risk is None:
            return {"content": None, "tool_calls": [{
                "id": "c2", "name": "check_shelf_life",
                "arguments": {"batch_no": "B20261030-01",
                              "planned_start_at": "2026-10-31T07:00:00+08:00"},
            }]}
        if not any(r.get("draft_id") for r in results):
            return {"content": None, "tool_calls": [{
                "id": "c3", "name": "create_suggestion_draft",
                "arguments": {"run_id": run["run_id"], "risk_id": risk["risk_id"],
                              "item_code": "M-CHK-001", "kg": run["suggestion"]["kg"]},
            }]}
        draft = next(r for r in reversed(results) if r.get("draft_id"))
        return {"content": (
            f"已为 300 份鸡胸肉生成备料草案 {draft['biz_no']}，建议用量 "
            f"{draft['kg']}kg（依据批次 B20261030-01 实测出成率）。"
            f"草案状态为“待确认”，需班组长本人在界面点击「确认备料」后生效，我不能代您确认。"
            f"确认入口：{draft['confirm_url']}"
        ), "tool_calls": []}


def part2_loop(conn, cfg) -> None:
    """用假的"模型"走 harness：校验 → 效期 → 强制中断 → 员工确认 → 草案建成。"""
    from prep_assistant.agents import harness

    print("\n=== 第二段：Harness 循环 + 建单强制中断 ===")
    session_id = "sess-demo-0002"
    pending_id = None
    round_no = 0
    for ev in harness.run(
        conn=conn,
        session_id=session_id,
        employee_no="emp001",
        question="帮我备 300 份鸡胸肉，用批次 B20261030-01",
        cfg=cfg,
        chat_fn=FakePlanner(),
    ):
        name, data = ev["event"], ev["data"]
        if name == "round":
            round_no = data["round_no"]
            print(f"\n  ── 第 {round_no} 轮模型决策 ──")
        elif name == "tool":
            flag = "ok" if data["ok"] else data.get("error_code")
            print(f"  [{flag}] {data['name']} ({data.get('duration_ms', 0)}ms)")
        elif name == "draft_preview":
            pending_id = data["action_id"]
            print("  ⏸  强制中断：create_suggestion_draft 需员工确认")
            print(f"     草案预览 → {data['item_name']} {data['portions']} 份"
                  f" = {data['kg']} kg（批次 {data['batch_no']}，效期 {data['risk_level']}）")
        elif name == "final":
            print(f"\n  停止原因：{data['stop_reason']}（共 {data['tool_calls']} 次工具调用）")

    if not pending_id:
        print("  （未触发中断，演示异常）")
        return

    print("\n  ── 员工点击「确认」──")
    result = harness.confirm_pending_action(
        conn, action_id=pending_id, session_id=session_id, employee_no="emp001", cfg=cfg,
    )
    print(f"  [通过] create_suggestion_draft → {result['biz_no']}（{result['kg']} kg）")
    print(f"  提示：{result['note']}")

    print("\n  ── 轮次日志（agent_round_logs，阶段 5 分析用）──")
    for row in agent_store.list_round_logs(conn, session_id):
        brief = row["result_summary"] or ""
        print(f"  第{row['round_no']}轮 {row['kind']:<10}"
              f"{row['tool_name'] or '-':<26} {brief[:48]}")


def main() -> int:
    if DEMO_DB.exists():
        DEMO_DB.unlink()
    conn = database.connect(DEMO_DB)
    database.init_schema(conn)
    agent_store.seed_demo_batches(conn)
    cfg = load_config()

    part1_gates(conn, cfg)
    part2_loop(conn, cfg)

    print(f"\n演示库：{DEMO_DB}（可删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
