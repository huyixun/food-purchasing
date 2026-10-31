# 接口说明

> 状态：员工问答（阶段 2）、管理后台（阶段 3）、智能体执行循环（阶段 4b）**已实现**；其余仍为规划草案。

## 已实现（阶段 4b：Agent Harness 执行循环）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/agent/ask` | POST | 自主决策入口，**SSE 流式**。事件序：`meta → round* → tool* → draft_preview? → answer* → done`；流内错误发 `event: error` |
| `/api/agent/draft/{action_id}/confirm` | POST | 员工确认草案（body: `{session_id}`）→ 真正执行 create_suggestion_draft；重复确认 409、跨会话 404、超时 410 |
| `/api/agent/draft/{action_id}/reject` | POST | 员工拒绝草案，不执行、不留单据 |

要点：

- **上限 5 轮**模型决策（`MAX_ROUNDS`），超限强制收口（`stop_reason=round_limit`）。
- **create_suggestion_draft 强制中断**：预校验（四道闸，不消费凭证）→ 通过则挂起
  `agent_pending_actions` 并推 `draft_preview`，员工确认后才执行（确认时再过完整闸门）。
- **轮次日志**：每轮模型决策与每次工具执行落 `agent_round_logs`（工具名/参数/耗时/
  结果摘要），供阶段 5 离线分析。
- `stop_reason`：`answered` / `no_content` / `round_limit` / `awaiting_confirmation`。
- 设计与轨迹示例见 `docs/agent-loop.md`；工具与凭证链见 `docs/agent-tooling-design.md`。

## 已实现（阶段 4：智能体工具层）

设计说明见 `docs/agent-tooling-design.md`。要点：

- 注册给模型的工具共 **8 个**，分 T0 只读 / T1 校验（签凭证）/ T2 草案三层；
  `confirm` / `accept` / `writeback` / `isolate` 四个动作**不注册**，只由前端按钮直调。
- 顺序约束由**凭证链**强制（HMAC + 会话归属 + 10 分钟 TTL + 一次性 + 参数哈希比对），
  不靠提示词。伪造凭证 `MISSING_PREREQUISITE`；改数 `PROVENANCE_MISMATCH`。
- `traces` 是完整工具轨迹，供审计与前端渲染建议卡片（数值以 `tool_result` 为准，不取模型文本）。
- 本入口与 `/api/chat` 共用 `session_id` 与 `conversations`/`messages` 表，可在同一会话里混用。

## 已实现（阶段 2：员工问答）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 聊天页（static/chat.html，原生 JS） |
| `/api/chat` | POST | 问答入口，SSE 流式；事件序 `meta → status → citations → delta* → done`，异常发 `error` |
| `/api/chat/{session_id}/history` | GET | 会话历史恢复（含引用、provider、prompt_version） |

错误契约：所有接口错误返回 `{"code", "message"}`（中文），流中错误走 `event: error`。
错误码：`INVALID_REQUEST` / `INVALID_QUESTION` / `SESSION_NOT_FOUND` / `RATE_LIMITED` / `LLM_UNAVAILABLE` / `LLM_TIMEOUT` / `INTERNAL_ERROR`。

## 已实现（阶段 3：管理后台）

登录：`POST /admin/login`（表单，成功种 HttpOnly Cookie `pa_token`，JWT HS256）；
`POST /admin/logout`。所有 `/admin/*` 需登录且 `role=admin`：页面未登录 302 跳登录页，JSON API 返回 401。
新增错误码：`UNAUTHORIZED` / `FORBIDDEN` / `INVALID_CREDENTIALS` / `UNSUPPORTED_FILE` / `FILE_TOO_LARGE` / `FILE_EMPTY` / `LAST_SOURCE`。

| 页面/接口 | 方法 | 说明 |
|---|---|---|
| `/admin` | GET | 总览（统计卡片） |
| `/admin/knowledge` | GET | 知识来源列表 |
| `/admin/knowledge/upload` | POST | 上传 md/pdf → pypdf 提取（pdf）→ 切片计数 → 落盘 data/knowledge/ → 元数据入库 → 索引失效，下次问答重建 |
| `/admin/knowledge/{id}/delete` | POST | 删除来源（最后一个默认拒绝，`admin.allow_delete_last_source` 可放开） |
| `/admin/prompts` | GET | 当前生效模板 + 历史版本 |
| `/admin/prompts/save` | POST | 保存新版本并立即生效（问答每次提问从库读模板；旧版本只增不删） |
| `/admin/audit` | GET | 审计列表；筛选：employee_no / date_from / date_to / only_flagged |
| `/admin/audit/{msg_id}/flag` | POST | 人工认定/取消违规（JSON） |
| `/admin/audit/{msg_id}/ai-review` | POST | LLM 辅助归因（手动触发；结果带「AI 辅助，仅供参考」标注） |

违规初筛：assistant 回答落库后自动跑确定性规则（`agents/moderation.py`，6 组正则：
计算数量 / 效期修改 / 批次放行 / 绕过确认 / 配方变更），命中落 `message_flags.rule_hits`，
干净也存 `[]` 表示「已扫描」。规则只做初筛，最终认定归管理员。

配置：`config/prototype.yaml` 的 `chat` 段（历史 7 轮、限流 10 次/60 秒、SQLite 路径）。
表结构：`conversations` + `messages` 两张表，见 `src/prep_assistant/infra/db.py`，阶段 3 原样迁 PG。

## 规划草案（未实现）

## 通用约定

- 认证：具名身份，禁止共用账号；写操作需在请求中携带确认人工号
- 时间：一律服务端时间，客户端传入的「现在」不予采信
- 幂等：写接口必须带 `Idempotency-Key`，由「业务编号 + 字段 + 版本」派生
- 版本：读接口返回 `version` / `etag`；写接口必须带 `If-Match`，不匹配返回 `409 VERSION_CONFLICT`
- 错误：业务拒绝返回结构化原因码，不使用 200 + 空数据

## 能力与接口草案

| 能力 | 方法与路径 | 说明 |
|---|---|---|
| F1 建议单生成 | `POST /suggestions:generate` | 入参：业务日期；出参：建议单（含六要素与字典版本） |
| F2 批次台账 | `GET /batches` · `POST /batches:scan` · `POST /batches/{id}:transition` | 扫码带入、状态流转，均需具名 |
| F3 短保提示 | `GET /suggestions/{id}/risks` | 返回触发原因与接手人，不给模糊提醒 |
| F4 确认 | `POST /suggestions/{id}/rows/{row}:confirm` | 动作：采用 / 微调 / 改判（改判必填原因枚举） |
| F5 交接 | `POST /handovers` · `POST /handovers/{id}:accept` | **接受动作才关闭待办**，仅发送消息不算 |
| F6 留痕 | `GET /audit/{businessId}` | 返回四段式记录，可重建该次处理 |
| F7 回写 | `POST /writeback:submit` · `GET /writeback/{id}` | 受两级授权约束；默认关闭 |
| F8 冲突裁决 | `GET /conflicts` · `POST /conflicts/{id}:resolve` | 双版本并列，裁决必填理由 |
| F9 数据健康 | `GET /health/report` · `GET /health/repair-queue` | 修复队列须含责任人与时限 |
| F11 回放 | `POST /replay:run` | 指定历史日期与字典版本，输出差异比对 |

## 待定

- 终端形态（Web / 工位平板）决定是否需要离线包与扫码适配层
- 与目标系统的写契约需联调后回填本节
