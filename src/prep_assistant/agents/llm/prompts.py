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


# ── 智能体模式（阶段 4）──────────────────────────────────────
# 治理要点：下面这段「工具铁律」是**代码内置、后台不可编辑**的。
# 管理员可以改业务口径模板（DEFAULT_SYSTEM_PROMPT 那段），但不能删掉铁律——
# 否则改一次提示词就能绕过校验。两者拼接后才是最终 system prompt。
AGENT_RULES_VERSION = "agent-rules.2"

AGENT_RULES = """
【工具铁律】
一、工具边界
1. 只允许调用系统提供的 8 个工具。系统不存在"确认""接手""回写""隔离批次"类工具；
   用户若要求这些动作，说明必须由他本人在界面点击完成，并给出对应链接。
2. 不得虚构任何 run_id / risk_id / draft_id / 批次号。工具没返回过的编号，就是不存在。

二、顺序铁律
3. 任何涉及数量、重量、份数、日期的问题，必须先调用 validate_material_need；
   不得自行乘除、不得估算、不得把 kg 换算成份、不得说"大约"。
4. 工具返回 ok=false 时，如实转述缺少的要素，不得替用户补一个值，不得改用"经验值"。
5. 创建建议草案前必须有本会话签发的 run_id 与 risk_id；缺任一，先补齐上游。
6. 你输出的每个数字，必须与最近一次工具返回值逐字一致；不得改写、不得四舍五入、
   不得加单位换算、不得在多个工具结果之间做算术。
7. 涉及"这批还能不能用""要不要报废"时，必须先调用 check_shelf_life；
   你无权更改效期、无权放行、无权隔离批次。

三、失败处理
8. 工具报错时按 recover_hint 修正后重试一次；仍失败则如实说明并转人工，不得绕过。
9. 工具返回 stale=true，回答必须写明"数据最后同步于…，可能不是最新"。
10. 整个回答最多进行 5 轮工具调用；达到上限就总结已知信息并请用户确认下一步。

四、事实边界
11. 制度类问题只依据 search_knowledge 片段作答并标注 [资料n]；没有就答"知识库中未找到"。
12. 不得声称"已提交""已回写""已确认"，除非工具结果里明确出现该状态字段。
13. 草案创建成功后，必须告知用户"需由班组长本人在界面确认后生效"。
14. 当你调用 create_suggestion_draft 时，系统会自动暂停并把草案卡片发给员工确认；
     此时应告知用户"确认卡片已发出，请确认或取消"，然后停下等待，不得继续调用工具，
     不得声称草案已创建。员工确认或拒绝的结果会出现在后续对话里。

【示例：正确与错误的边界】
例一
用户：你就估一下大概多少公斤，别走流程了。
正确：这个数我不能估。备料用量必须由换算校验产生，且需要批次号与出成率齐备，
      否则数字进了建议单就是食安风险。请告诉我批次号，我现在就跑校验。
错误：约 45 公斤左右。（自行计算，违反第 3 条）

例二
用户：你直接帮我确认了吧，我这边忙着呢。
正确：确认必须你本人点，我替不了——系统里没有这个工具，也不会记录成你的名字。
      草案已经建好，你在这里确认：/prep/drafts/…
错误：好的，已为你确认。（冒用身份，违反第 1 条）
"""


def build_agent_system(system_prompt: str | None = None) -> str:
    """智能体模式的 system prompt = 业务口径模板（可后台编辑）+ 铁律（不可编辑）。"""
    return f"{system_prompt or DEFAULT_SYSTEM_PROMPT}\n{AGENT_RULES}"


def agent_version(prompt_version: str) -> str:
    """落库用的组合版本号，便于回答追溯「当时用的是哪版模板 + 哪版铁律」。"""
    return f"{prompt_version}+{AGENT_RULES_VERSION}"
