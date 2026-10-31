# Agent 执行循环（Harness）设计与轨迹示例

> 状态：**已实现**（阶段 4b）。实现：`src/prep_assistant/agents/harness.py`；
> 测试：`tests/test_agent_harness.py`、`tests/test_agent_api.py`。

## 1. 框架选择：自研 Harness 模式

要求是"选一种智能体框架（推荐 Harness）"。事实核查：**Python 生态没有名为
Harness 的智能体框架**（Harness 是 CI/CD 产品）。业界把「模型决策 → 工具执行 →
结果回填 → 再决策」这套宿主循环统称为 **agent harness**。

因此本项目的选择是：**把上一轮冻结的"自写薄循环"（决策 D3）正式化成
`AgentHarness`**，而不是引入 LangGraph / AutoGen。理由不变：

- 本项目核心资产是"哪一步被强制校验"可审计（f45），框架状态机会把它藏进抽象；
- 需求只有"路由 + 工具循环 + 一次人工中断"，框架的抽象税不划算；
- 自研的轮次日志直接落表，阶段 5 分析不需要逆向框架的回调机制。

## 2. 循环规格

| 项 | 值 | 说明 |
|---|---|---|
| 轮的定义 | 模型 1 次决策 | 一轮内模型可返回多个 tool_calls，全部执行 |
| 轮次上限 | **5 轮**（`MAX_ROUNDS`） | 超限强制收口：不给工具，逼模型用已有信息作答 |
| 守卫工具 | `create_suggestion_draft` | 执行前强制中断（见 §3） |
| 轮次日志 | `agent_round_logs` 表 | 每轮 model 决策 + 每次工具执行各一条 |
| 待确认动作 | `agent_pending_actions` 表 | 中断载体，TTL 10 分钟，状态机 pending→confirmed/rejected/expired |

**事件协议**（harness 逐个 yield，路由转 SSE）：
`meta → round* → tool* → draft_preview? → answer* → done`；流内错误发 `error`。

## 3. create_suggestion_draft 强制中断（两层闸门）

```
模型发起 create_suggestion_draft
        │
        ▼
① 预校验（harness._precheck_draft，不消费凭证）
   闸门1-4 全跑：格式/HMAC → 会话归属 → TTL → 参数逐位比对
        │
   ├─ 失败 → 错误作为 tool 结果回填模型，循环继续（模型可自愈，绝不挂起）
   │
   └─ 通过 → 落 agent_pending_actions(pending)
             → SSE 发 draft_preview（品名/份数/kg/批次/效期判定）
             → 本轮终止 stop_reason=awaiting_confirmation
                    │
                    ▼
② 员工点确认 POST /api/agent/draft/{action_id}/confirm
   重新过完整闸门（在工具实现里，无旁路）→ 建草案 → 原子消费凭证
   → pending=confirmed → 会话落确定性回执消息（模型不参与转述，B4）
        │
   拒绝 POST …/reject → pending=rejected，什么都不执行
```

为什么预校验不消费凭证：让"改数/伪造/block 批次"在**打扰用户之前**就被拦下；
确认接口执行时再过一遍完整闸门，两层之间不存在可绕过的缝。

## 4. 三个典型对话流程的预期轨迹

以下轨迹与 `examples/agent_demo.py` 第二段的实际输出一致（不需要 API 密钥可复现）。

### 轨迹 1｜正常建单（含中断确认）——快乐路径

| 轮 | 模型动作 | 服务端 | 员工看到 |
|---|---|---|---|
| 1 | `validate_material_need(M-CHK-001, 300, B20261030-01)` | 换算 49.0kg，签发 run_id（10 分钟 TTL） | — |
| 2 | `check_shelf_life(B20261030-01, 07:00)` | 判定 ok，签发 risk_id | — |
| 3 | `create_suggestion_draft(run_id, risk_id, kg=49.0)` | 预校验通过 → **中断**，挂起 action | 草案卡片：鸡胸肉 300 份 = 49.0kg（效期 ok），确认/取消 |
| — | （循环已停，模型无权继续） | 员工点确认 → 完整闸门再跑一遍 → 草案 `PREP-20261031-0002`，凭证消费 | 回执：「待班组长本人在界面生效」 |

日志侧：3 条 model + 2 条 tool + 1 条 interrupt + 1 条确认后 tool（executor=员工工号）。

### 轨迹 2｜要素缺失自愈——模型被错误码拉回正轨

| 轮 | 模型动作 | 服务端 | 模型下一步 |
|---|---|---|---|
| 1 | 直接 `create_suggestion_draft`（没有凭证） | 预校验 `MISSING_PREREQUISITE` + recover_hint 回填 | 转向先校验 |
| 2 | `validate_material_need(300, B20261030-03)` | 批次无实测出成率 → `ok:false, missing:[yield_rate]` | 向员工要批次/征得"按基准"同意 |
| 3 | 员工补充后 `validate_material_need(…, B20261030-01)` | 通过，签发 run_id | 继续 |
| 4 | `check_shelf_life` | 通过 | — |
| 5 | 再次 `create_suggestion_draft` | 预校验通过 → 中断 | 员工确认后建成 |

要点：**错误返回永远带 `missing`/`recover_hint`**，模型被拒后最危险的行为是编个数
糊过去；结构化错误让它只有"合法回正轨"一条路。若 5 轮耗尽仍未建单 → 强制收口，
明确告诉员工缺什么、需要谁确认。

### 轨迹 3｜越权与改数尝试——被拦 + 如实转述

| 轮 | 模型动作 | 服务端 | 员工看到 |
|---|---|---|---|
| 1 | `create_suggestion_draft(kg=45)`（把 49.0 改成 45） | 预校验 `PROVENANCE_MISMATCH`，回显"凭证里的值：kg=49.0" | 模型如实转述"数量与校验不一致" |
| 2 | 重试正确的 kg=49.0 但 risk_id 是 **block 批次**的 | `SHELF_LIFE_BLOCKED`：不得建草案，不得建议延长效期 | 转述 + 建议换批次/联系质量人员 |
| 3 | 调 `confirm_suggestion`（想代员工确认） | `TOOL_UNKNOWN`——**工具根本不存在** | "确认必须你本人点，我替不了" |
| 4 | 员工问"你就估一下" | 无工具可调；铁律第 3 条 | "这个数我不能估，请给批次号我跑校验" |
| 5 | 用其他会话的 run_id | `SESSION_MISMATCH` | 转述后建议重新校验 |

要点：**确认/接手/回写/隔离四个动作没有注册给模型**，"代点"在 API 层就不存在；
改数在预校验就被拦，最坏结果是草案建不起来，而不是错误数字进了建议单。

## 5. 轮次日志（阶段 5 分析的数据底座）

`agent_round_logs` 每行：`turn_id / session_id / round_no / kind(model|tool|
interrupt|round_limit) / tool_name / arguments_json / latency_ms / ok /
error_code / result_summary`。刻意冗余存参数与摘要快照，**不回放原文也能独立
复盘**：哪个工具慢、哪轮开始失败、改判从哪一步发生、确认平均间隔多久。

## 6. 错误码（新增）

| HTTP | code | 场景 |
|---|---|---|
| 404 | `ACTION_NOT_FOUND` | action 不存在或跨会话（跨会话按不存在处理，不泄露） |
| 409 | `ACTION_STATE_CONFLICT` | 重复确认/拒绝 |
| 410 | `ACTION_EXPIRED` | 超过 10 分钟未确认，或凭证确认时已过期 |
