"""提示词模板（★⑤，阶段 3 版：模板入库存数据库，代码只保留默认值）。

管理约定：
- 默认模板 = 管理员从未改过时的出厂值（决策：措辞按备料口径，
  由用户给的模板原句改「报销标准」→「备料/换算标准」而来）
- 管理员保存新模板 → prompt_templates 表插入新版本并置为生效，
  旧版本只增不删（对齐决策记录「字典版本只增不改」的纪律）
- 问答路由**每次提问**都从库里读当前生效版本——修改后立即生效，
  不需要重启服务；messages.prompt_version 记录当次用的版本号
"""

from __future__ import annotations

import sqlite3

from prep_assistant.infra.db import get_active_template

# 出厂默认模板（备料口径）。管理员在后台保存的第一个版本基于它修改。
DEFAULT_SYSTEM_PROMPT = """你是公司备料助手，只根据提供的制度条文回答，不确定时明确说"知识库中未找到"，回答末尾附引用来源，不得编造备料与换算标准。
回答规则：
1. 只依据【参考资料】回答，每个论断后面标注来源，如 [资料2]；
2. 资料里没有的，直接回答"知识库中未找到"，不要编造；
3. 禁止计算或推断任何数量、重量、日期等数值，此类问题请引导用户使用备料功能；
4. 涉及操作建议时，提醒以班组长确认结果为准；
5. 【历史对话】仅供理解上下文，不得从中编造制度内容。"""


def resolve_system_prompt(conn: sqlite3.Connection) -> tuple[str, str]:
    """取当前生效的（模板, 版本号）；库中无生效版本时回退出厂默认。

    返回 (system_prompt, version)，version 形如 "default" / "v3"。
    """
    row = get_active_template(conn)
    if row is None:
        return DEFAULT_SYSTEM_PROMPT, "default"
    return row["content"], row["version"]


def build_messages(
    question: str,
    hits: list[dict],
    history: list[dict] | None = None,
    system_prompt: str | None = None,
) -> list[dict]:
    """组装 LLM 消息序列：system → 历史轮次 → 带引用的当前问题。

    system_prompt 不传则用出厂默认（CLI/测试场景）；Web 路由每次
    提问都会先 resolve_system_prompt() 把库里的当前版本传进来。
    history 形如 [{"role": "user"|"assistant", "content": "..."}]。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT}]
    for item in history or []:
        messages.append({"role": item["role"], "content": item["content"]})
    refs = "\n\n".join(f"[资料{i + 1}]\n{h['text']}" for i, h in enumerate(hits))
    messages.append({"role": "user", "content": f"【参考资料】\n{refs}\n\n【问题】{question}"})
    return messages
