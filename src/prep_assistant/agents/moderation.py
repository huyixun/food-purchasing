"""违规问答初筛：确定性规则，不是 LLM（决策 B4：判定类结论必须可重建）。

工作方式：每条 assistant 回答落库后，对「问题+回答」跑一遍规则表，
命中的规则以 JSON 落 message_flags.rule_hits，审计页标红并列出命中词。
LLM 辅助归因（决策 B：规则＋LLM）是另一条独立链路，由管理员在审计页
手动触发，结果存 ai_review 并永远标注「AI 辅助，仅供参考」。

规则维护约定：新增/调整规则只需改 RULES 表，规则 id 稳定后不要改，
审计记录里存的是规则 id + 命中原文。
"""

from __future__ import annotations

import json
import re

# 规则表：(规则 id, 展示名, 正则模式列表)
# 场景口径为备料（非报销）：抓「引导系统做红线动作」的问答
RULES: list[tuple[str, str, list[str]]] = [
    (
        "calc_quantity",
        "要求计算数量/换算",
        [r"(备|需要|要).{0,8}(多少|几)[公斤千克kg克g份箱件包袋]", r"算(一)?(下|算)?(需要|用)"],
    ),
    (
        "modify_shelf_life",
        "涉及效期修改",
        [r"(改|修改|延(长)?|推迟).{0,6}(效期|保质期|有效期)", r"效期.{0,6}(改|延长)"],
    ),
    (
        "release_batch",
        "涉及批次放行/解除隔离",
        [r"(放行|解封|解除隔离|恢复使用).{0,8}(批次|异常|冻品|原料)?", r"异常批次.{0,8}(继续|照常|还是能用)"],
    ),
    (
        "bypass_confirm",
        "绕过确认/交接流程",
        [r"(不|别|先不用|不用)(走|做|等)(确认|交接|审批|流程)", r"(先|直接)(备|用|发).{0,10}(再|回头)(补|说)"],
    ),
    (
        "recipe_change",
        "涉及配方变更",
        [r"(换|改|替)成.{0,10}(配方|别的料|其他原料)", r"配方.{0,6}(改|调整)"],
    ),
]

_COMPILED = [(rid, label, [re.compile(p) for p in patterns]) for rid, label, patterns in RULES]


def scan_pair(question: str, answer: str) -> list[dict]:
    """对一问一答跑全部规则，返回命中列表。

    命中判据同时扫问题与回答：员工的问题诱导（「帮我算下备多少」）和
    模型的越界回答（真的给出了数量）都算疑似违规，交管理员人工复核。
    返回 [] 表示干净。
    """
    hits: list[dict] = []
    for rid, label, patterns in _COMPILED:
        matched = []
        for pat in patterns:
            for m in pat.finditer(question):
                matched.append(f"问题:…{m.group(0)}…")
            for m in pat.finditer(answer):
                matched.append(f"回答:…{m.group(0)}…")
        if matched:
            hits.append({"rule": rid, "label": label, "hits": matched[:6]})  # 每条规则最多留 6 个命中示例
    return hits


def hits_to_json(hits: list[dict]) -> str:
    """命中列表 → 落库 JSON（空列表也要存，表示「已扫描且干净」）。"""
    return json.dumps(hits, ensure_ascii=False)


def ai_review_messages(question: str, answer: str, rule_hits: list[dict]) -> list[dict]:
    """构造 LLM 辅助归因的消息序列（管理员手动触发时使用）。

    只让模型做「归因说明」，不给它判定权——输出永远标注「AI 辅助，仅供参考」。
    """
    rule_text = (
        "\n".join(f"- {h['label']}（规则 {h['rule']}）：{'；'.join(h['hits'])}" for h in rule_hits)
        or "（规则初筛未命中）"
    )
    system = (
        "你是备料助手运营团队的合规复核助手。给你一段员工与系统的问答，"
        "以及规则初筛结果。请用不超过 120 字说明：这段问答为什么值得（或不值得）"
        "人工复核，风险点是什么。只做归因说明，不做最终定性，不提出处罚建议。"
        "若无明显风险，直接说明可以放行。"
    )
    user = f"【员工提问】{question}\n\n【系统回答】{answer}\n\n【规则初筛】\n{rule_text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
