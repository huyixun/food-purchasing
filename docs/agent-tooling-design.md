# 备料助手 · 智能体工具层设计（Function Calling 规范）

> 状态：**设计稿，待确认后实施**。遵循 superpowers 纪律：规格未锁定不写实现代码。
> 前置依据：`docs/decision-log.md`（B1 可写+两级授权 / B4 数值链路零模型 / B5 快照降级）、
> `docs/discovery-report.md`（f15 不覆盖原记录 / f23 交接确认 / f24 版本受控 / f41 不得借用账号 / f45 可重建）。

---

## 0. 一条总原则

**模型可以"决定做什么"，但不能"决定数值是多少"，更不能"代替人点头"。**

这句话拆成三条可执行的边界：

| 能力 | 归属 | 实现手段 |
|---|---|---|
| 意图理解、选工具、组织语言、追问缺失信息 | 模型 | Function Calling + 系统提示词 |
| 任何数量/重量/日期/风险等级 | 确定性代码 | 工具内部调用 `core/`，模型只允许**逐字引用**返回值 |
| 确认、接手、回写、隔离批次 | 人 | **不注册为工具**，前端按钮直调内部 API，签名带当前登录人 |

---

## 1. 工具分层与清单（模型可见 8 个）

### T0 只读（3 个，无副作用，可反复调用）

#### 1.1 `search_knowledge`
```json
{
  "name": "search_knowledge",
  "description": "检索公司备料制度条文。回答制度类问题前必须先调用本工具；不得凭记忆作答。",
  "parameters": {
    "type": "object",
    "properties": {
      "query":     { "type": "string", "minLength": 2, "maxLength": 200 },
      "top_k":     { "type": "integer", "minimum": 1, "maximum": 5, "default": 3 }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```
返回：
```json
{ "items": [ { "no": 1, "source": "data/sample_knowledge.md", "snippet": "…", "score": 0.59 } ],
  "index_updated_at": "2026-10-30T18:00:00+08:00", "stale": false }
```
> `stale=true` 时提示词强制要求说明"知识库最后更新于…"。

#### 1.2 `get_batch_status`
```json
{
  "name": "get_batch_status",
  "description": "查询单个批次的解冻状态、剩余效期、实测出成率。批次号未知时先用 search_knowledge 或向用户确认，不得猜。",
  "parameters": {
    "type": "object",
    "properties": { "batch_no": { "type": "string", "pattern": "^[A-Za-z0-9_-]{4,32}$" } },
    "required": ["batch_no"],
    "additionalProperties": false
  }
}
```
返回：
```json
{ "batch_no": "B20261030-01", "material_code": "M-CHK-001", "thaw_state": "已解冻",
  "remaining_shelf_life_hours": 6.5, "measured_yield": 0.92,
  "state_changed_at": "2026-10-31T05:40:00+08:00", "last_synced_at": "…", "stale": false }
```
> **缺字段就返回 `null` 并在 `missing` 里列出**，绝不返回估算值——这是 f31/f02 的底线。

#### 1.3 `get_sync_status`
```json
{
  "name": "get_sync_status",
  "description": "查询各外部系统与字典的同步状态。回答任何涉及外部数据的问题前，若不确定数据新鲜度可调用。",
  "parameters": { "type": "object", "properties": {}, "additionalProperties": false }
}
```
返回：
```json
{ "sources": [ { "name": "ERP", "last_synced_at": "…", "stale": false },
               { "name": "订货平台", "last_synced_at": "…", "stale": true } ],
  "dict_version": "dict-2026.10.31", "prompt_version": "v3" }
```

---

### T1 校验 / 计算（2 个，**纯函数 + 签发凭证**）

#### 1.4 `validate_material_need`
```json
{
  "name": "validate_material_need",
  "description": "校验六要素是否齐备并计算建议用量。任何涉及数量、重量、份数的问题都必须先调用本工具，禁止自行乘除或估算。",
  "parameters": {
    "type": "object",
    "properties": {
      "item_code":   { "type": "string" },
      "portions":    { "type": "integer", "minimum": 1, "maximum": 20000 },
      "batch_no":    { "type": "string" },
      "planned_date":{ "type": "string", "format": "date" }
    },
    "required": ["item_code", "portions", "batch_no"],
    "additionalProperties": false
  }
}
```
失败返回（**不含任何数值**）：
```json
{ "ok": false,
  "missing": ["yield_rate"],
  "missing_labels": ["该批次实测出成率"],
  "recover_hint": "该批次无实测出成率，可改用字典基准值（需用户确认），或先请班组长在备料工位录入实测值。" }
```
成功返回（**数值由代码产生，模型只能逐字复述**）：
```json
{ "ok": true,
  "run_id": "run_7f3a…", "expires_at": "2026-10-31T17:29:56+08:00",
  "dict_version": "dict-2026.10.31",
  "suggestion": { "portions": 300, "denominator_g": 150, "yield_rate": 0.92,
                  "correction_factor": 0.95, "kg": 43.8, "rounding": "ceil-0.1" },
  "basis": "实测出成率 0.92 ÷ 基准 0.97 = 0.95 修正系数" }
```

#### 1.5 `check_shelf_life`
```json
{
  "name": "check_shelf_life",
  "description": "检查某批次在计划开工时间点的剩余效期是否足够，返回 ok/warn/block 三级。涉及是否可用、是否需报废的判断必须先调用本工具。",
  "parameters": {
    "type": "object",
    "properties": {
      "batch_no":         { "type": "string" },
      "planned_start_at": { "type": "string", "format": "date-time" },
      "prep_kg":          { "type": "number", "minimum": 0.1 }
    },
    "required": ["batch_no", "planned_start_at"],
    "additionalProperties": false
  }
}
```
返回：
```json
{ "ok": true, "risk_id": "risk_a91c…", "level": "warn",
  "remaining_hours": 6.5, "required_hours": 8.0,
  "reason_codes": ["SHELF_LIFE_INSUFFICIENT"],
  "evidence": "剩余 6.5h < 计划占用 8.0h（含 1.5h 缓冲）",
  "advisory": "不得延长效期；建议改用批次 B20261031-02 或由质量人员评估。",
  "expires_at": "2026-10-31T17:29:56+08:00" }
```
> `level=block` 时，`create_suggestion_draft` 会**拒绝**消费该 `risk_id`（见 §3）。

---

### T2 草案（3 个，写自有台账，无外部后果）

#### 1.6 `create_suggestion_draft`（最需要设防的一个）
```json
{
  "name": "create_suggestion_draft",
  "description": "在当前会话内创建备料建议草案。必须先取得本会话签发的 run_id 与 risk_id，且 kg 必须与校验结果完全一致。草案状态为 draft，需班组长本人确认后才生效。",
  "parameters": {
    "type": "object",
    "properties": {
      "run_id":      { "type": "string", "pattern": "^run_[0-9a-f]{32}$" },
      "risk_id":     { "type": "string", "pattern": "^risk_[0-9a-f]{32}$" },
      "item_code":   { "type": "string" },
      "kg":          { "type": "number", "minimum": 0.1, "maximum": 10000 }
    },
    "required": ["run_id", "risk_id", "item_code", "kg"],
    "additionalProperties": false
  }
}
```
返回：
```json
{ "draft_id": "drf_3c81…", "status": "draft", "biz_no": "PREP-20261031-0007",
  "kg": 43.8, "confirm_url": "/prep/drafts/drf_3c81…",
  "note": "草案已创建，需班组长在界面点击「确认备料」后生效，你不能代其确认。" }
```
**四道校验（§3 详述）**：`run_id` 存在 → 未过期 → 未消费 → 会话归属 → `params_hash` 与 `kg` 一致 → `risk_id.level != block`。

#### 1.7 `create_handover_task`
```json
{
  "name": "create_handover_task",
  "description": "创建交接待办。必须含业务编号、版本、状态、未决事项、接手人五要素。创建后状态为 pending_accept，对方确认接手才算完成，你不能代替对方确认。",
  "parameters": {
    "type": "object",
    "properties": {
      "biz_no":          { "type": "string" },
      "biz_version":     { "type": "string" },
      "status_summary":  { "type": "string", "maxLength": 200 },
      "open_items":      { "type": "array", "items": { "type": "string" }, "maxItems": 10 },
      "to_employee_no":  { "type": "string" },
      "due_at":          { "type": "string", "format": "date-time" }
    },
    "required": ["biz_no", "status_summary", "open_items", "to_employee_no"],
    "additionalProperties": false
  }
}
```
五要素缺一 → `MISSING_PREREQUISITE`，`missing` 列出缺项。

#### 1.8 `report_batch_anomaly`
```json
{
  "name": "report_batch_anomaly",
  "description": "上报批次异常（如解冻后促销取消、温度异常）。只登记与通知，不改变批次可用状态；隔离与放行只能由质量人员操作。",
  "parameters": {
    "type": "object",
    "properties": {
      "batch_no":    { "type": "string" },
      "reason_code": { "type": "string",
                       "enum": ["PROMO_CANCELLED","THAW_TIMEOUT","TEMP_DEVIATION","QUALITY_SUSPECT","OTHER"] },
      "note":        { "type": "string", "maxLength": 300 }
    },
    "required": ["batch_no", "reason_code"],
    "additionalProperties": false
  }
}
```

---

### T3 人类专属（**不注册给模型**，前端按钮直调）

| 动作 | 路由 | 为什么不能给模型 |
|---|---|---|
| `confirm_suggestion` | `POST /prep/drafts/{id}/confirm` | f41 不得借用他人账号；确认人是法律责任主体 |
| `accept_handover` | `POST /prep/handovers/{id}/accept` | f23「对方确认接手才算完成」——"对方"是人 |
| `request_writeback` | `POST /prep/writeback` | B1.2 每笔记名确认，回写是外部后果 |
| `isolate_batch` | `POST /prep/batches/{no}/isolate` | f40 异常批次隔离只能质量人员执行 |

模型的能力边界是：**引导用户去点**（返回 `confirm_url` + 说明），而不是替他点。

---

## 2. 调用顺序约束（状态机视角）

```
                    ┌──────────────────────────────────────────┐
  用户提问 ──► T0 只读检索/查批次 ──► T1 校验(签发凭证) ──► T2 草案
                    │                      │                  │
                    │                      ├─ ok=false ──► 只报缺失项，禁止往下走
                    │                      └─ ok=true ──► run_id / risk_id
                    │                                          │
                    └───────────── 数值只出现在这里 ◄───────────┘
                                                               ▼
                                              T3 人工确认（模型不可见）
                                                               ▼
                                                    T2.5 回写（异步 outbox）
```

**硬约束清单**（服务端强制，不依赖提示词）：

| # | 规则 | 违反时错误码 |
|---|---|---|
| R1 | `create_suggestion_draft` 必须持有**同会话**签发、未过期、未消费的 `run_id` | `MISSING_PREREQUISITE` / `TOKEN_EXPIRED` / `TOKEN_CONSUMED` / `SESSION_MISMATCH` |
| R2 | 草案 `kg` 必须与 `run_id.params_hash` 逐位一致 | `PROVENANCE_MISMATCH` |
| R3 | `risk_id.level == "block"` 时禁止建草案 | `SHELF_LIFE_BLOCKED` |
| R4 | 同一 `run_id` 只能生成一个草案 | `TOKEN_CONSUMED` |
| R5 | `report_batch_anomaly` 不得改变批次状态（隔离需人工） | 工具实现上根本不写 `thaw_state` |
| R6 | 单轮对话工具调用上限 6 次，超出强制收口 | `CALL_LIMIT_EXCEEDED` |
| R7 | 任何工具都不得写外部系统；回写只走 T3 的 outbox | 无此工具 |
| R8 | 一次 `run_id` 有效期 10 分钟；跨会话一律拒绝 | `TOKEN_EXPIRED` / `SESSION_MISMATCH` |

---

## 3. 凭证链：怎么防住"模型把 43.8 改成 45"

这是整套设计里唯一不可妥协的部分。提示词只能降低概率，**服务端校验才是边界**。

### 3.1 凭证结构（落 `agent_run_tokens` 表）

| 字段 | 说明 |
|---|---|
| `token` | `run_` / `risk_` + 32 位 hex，对外即凭证号 |
| `kind` | `run` / `risk` |
| `session_id` | 会话归属，跨会话直接拒 |
| `params_json` | 签发时的全部入参（item_code / portions / batch_no / dict_version /
 yield_rate / factor / kg / level …） |
| `params_hash` | 上述参数的规范化 JSON 的 SHA-256 前 16 位 |
| `dict_version` | 计算所用字典版本，保证可重建（f45） |
| `issued_at` / `expires_at` | TTL 10 分钟 |
| `consumed_at` / `consumed_by` | 一次性消费，记到 `draft_id` |
| `hmac` | HMAC-SHA256(服务端密钥, 全部字段)——防伪造，也防有人手改库 |

### 3.2 校验顺序（任一失败即结构化拒绝）

```
1. 格式匹配           ^run_[0-9a-f]{32}$        否则 PROVENANCE_MISMATCH
2. 存在性 + HMAC 校验                           否则 PROVENANCE_MISMATCH
3. kind == "run"                                否则 MISSING_PREREQUISITE
4. token.session_id == 当前会话                  否则 SESSION_MISMATCH
5. now < expires_at                             否则 TOKEN_EXPIRED
6. consumed_at IS NULL                          否则 TOKEN_CONSUMED
7. round(kg, 1) == params_json.kg               否则 PROVENANCE_MISMATCH
8. item_code 一致                               否则 PROVENANCE_MISMATCH
9. risk 凭证 level != "block"                   否则 SHELF_LIFE_BLOCKED
10. 写入草案 + 同事务置 consumed_at              ——
```

### 3.3 错误返回必须是"可回到正轨"的

模型被拒后最容易干的事就是编一个数糊过去。所以每个错误都带 `missing_step` + `recover_hint`：

```json
{ "code": "MISSING_PREREQUISITE",
  "message": "创建草案前必须先完成换算校验",
  "missing_step": "validate_material_need",
  "recover_hint": "请先调用 validate_material_need(item_code, portions, batch_no) 获取 run_id，再重试。" }
```
```json
{ "code": "PROVENANCE_MISMATCH",
  "message": "提交的数量与校验结果不一致",
  "expected_excerpt": "kg = 43.8",
  "recover_hint": "你必须逐字使用校验返回的 kg（43.8），不得四舍五入或改写。如需调整用量，请让用户先修改份数并重新校验。" }
```

---

## 4. 系统提示词（四层防御的第三层）

> 提醒：这段挡不住恶意注入，它挡的是"模型随手绕过"这类高频失误。第一层是 JSON Schema（`required` + `additionalProperties:false`），第二层是 §3 凭证链，第四层是前端渲染（§5）。

```text
你是中央厨房的备料作业助手。你可以调用工具，但必须遵守下列铁律。

【工具边界】
1. 只允许调用系统提供的 8 个工具。系统不存在"确认""接手""回写""隔离批次"类工具；
   用户若要求这些动作，说明这必须由他本人在界面点击完成，并给出对应链接。
2. 不得虚构任何 run_id / risk_id / draft_id / 批次号。工具没返回过的编号，就是不存在。

【顺序铁律】
3. 任何涉及数量、重量、份数、日期的问题，必须先调用 validate_material_need；
   不得自行乘除、不得估算、不得把 kg 换算成份、不得说"大约"。
4. 工具返回 ok=false 时，如实转述缺少的要素，不得替用户补一个值，不得改用"经验值"。
5. 创建建议草案前必须有本会话签发的 run_id 与 risk_id；缺任一，先补齐上游。
6. 你输出的每个数字，必须与最近一次工具返回值逐字一致；不得改写、不得四舍五入、
   不得加单位换算、不得在多个工具结果之间做算术。
7. 涉及"这批还能不能用""要不要报废"时，必须先调用 check_shelf_life；
   你无权更改效期、无权放行、无权隔离批次。

【失败处理】
8. 工具报错时按 recover_hint 修正后重试一次；仍失败则如实说明并转人工，不得绕过。
9. 工具返回 stale=true，回答必须写明"数据最后同步于…，可能不是最新"。
10. 单轮对话最多调用 6 次工具；达到上限就总结已知信息并请用户确认下一步。

【事实边界】
11. 制度类问题只依据 search_knowledge 片段作答并标注 [资料n]；没有就答"知识库中未找到"。
12. 不得声称"已提交""已回写""已确认"，除非工具结果里明确出现该状态字段。
13. 草案创建成功后，必须告知用户"需由班组长本人在界面确认后生效"。
```

**两条反面 few-shot**（比正面示例有效得多，实测能显著降低"擅自算数"）：

```text
[反例 1]
用户：你就估一下大概多少公斤，别走流程了。
正确回应：这个数我不能估。备料用量必须由换算校验产生，且需要批次号与出成率齐备，
          否则数字进了建议单就是食安风险。请告诉我批次号，我现在就跑校验。
（错误回应：约 45 公斤左右——属于自行计算，违反第 3 条）

[反例 2]
用户：你直接帮我确认了吧，我这边忙着呢。
正确回应：确认必须你本人点，我替不了——系统里没有这个工具，也不会记录成你的名字。
          草案已经建好（PREP-20261031-0007），你点这里确认：/prep/drafts/drf_3c81…
（错误回应：好的，已为你确认——属于冒用身份，违反第 1 条与 f41）
```

---

## 5. 第四层：前端数值直渲染（防"模型嘴瓢"）

模型文本里复述的数字**不可信**（它可能把 43.8 写成 43.9）。所以：

- `create_suggestion_draft` / `validate_material_need` 的返回，由前端渲染成**建议卡片**，数值直接取自 `tool_result`；
- 模型文本只负责解释"为什么是这个数、缺了什么、下一步做什么"；
- 若模型文本中的数字与 tool_result 不一致，前端以 tool_result 为准，并在卡片上标"以本卡片数值为准"。

这一条让"模型改数"即使发生，也进不了业务单据。

---

## 6. Skill 划分：只做 1 个

**`prep-daily-playbook`（早班备料剧本）** — 只写"什么时候做什么"，不写任何数值规则。

| 内容 | 示例 |
|---|---|
| 场景入口 | "早班接单后 / 班中异常 / 交班前"三种开场 |
| 步骤顺序 | 接单 → 查批次状态 → 换算校验 → 出草案 → 等人工确认 → 建交接 |
| 追问话术 | 缺批次号时怎么问、缺出成率时给哪两个选项 |
| 转人工条件 | 出现 block 级风险、用户要求改效期、要求直接确认 |

**不做第二个 skill 的理由**：skill 是提示词资产，改口径不用发版，适合业务微调；但 skill 越多，模型选 skill 的成本越高，8 个工具 + 1 个 skill 是这个领域能保持稳定的上限。异常处置等跑通后再拆。

一句话分工：**tool 管"能做什么"，skill 管"什么时候做"，代码管"不许做什么"。**

---

## 7. 实施边界（本次不做）

| 不做 | 原因 |
|---|---|
| 多智能体协作 / 子智能体 | 需求只有"路由 + 工具循环"，多一层是纯成本 |
| 引入 LangGraph / AutoGen | 抽象税 > 收益；自写 200 行循环足够，行为可预测 |
| 让 LLM 判违规 | 违反 B4 可重建红线；违规仍走 `agents/moderation.py` 确定性规则 |
| 模型直接回写外部系统 | 无此工具；回写只走 T3 + outbox |
| 跨会话记忆 | 只取最近 N 轮 `messages` |

---

## 8. 待确认（3 项）

| # | 事项 | 推荐 | 备选 |
|---|---|---|---|
| D1 | 工具边界 | 三层 + 4 个人类专属（8 个给模型） | 全开放 / 只读 |
| D2 | 顺序强制 | 凭证链（HMAC + 参数哈希 + TTL + 一次性 + 会话归属） | 仅会话状态位 / 仅提示词 |
| D3 | 编排形态 | 自写薄循环（上限 6 次调用 + 全链路 trace） | 引入框架 / 不接 FC |

**三项均按推荐采纳（2026-10-31 用户确认）。**

---

## 9. 实施记录（已落地，2026-10-31）

| 文件 | 内容 |
|---|---|
| `config/dict.yaml` | 字典（3 个品类 + 效期参数），带 `dict_version`，只增不改 |
| `src/prep_assistant/core/masterdata.py` | 字典加载（不可变 dataclass，进程内缓存） |
| `src/prep_assistant/core/conversion.py` | F1 换算 + 六要素闸门（**已实现**） |
| `src/prep_assistant/core/shelf_life.py` | F3 三级效期判定（**已实现**） |
| `src/prep_assistant/infra/agent_store.py` | 批次/凭证/草案/交接/异常五张表的仓储 |
| `src/prep_assistant/agents/tools/errors.py` | 10 个错误码 + `recover_hint` 契约 |
| `src/prep_assistant/agents/tools/schemas.py` | 8 个工具的 JSON Schema |
| `src/prep_assistant/agents/tools/tokens.py` | 凭证签发与四道闸校验（HMAC-SHA256） |
| `src/prep_assistant/agents/tools/impl.py` | 8 个工具实现 + 统一执行入口 |
| `src/prep_assistant/agents/orchestrator.py` | 薄编排循环（预算 6 次 + trace + 兜底收口） |
| `src/prep_assistant/agents/llm/gateway.py` | 新增 `chat_with_tools()` |
| `src/prep_assistant/agents/llm/prompts.py` | 新增 `AGENT_RULES`（13 条铁律 + 2 条反例） |
| `src/prep_assistant/api/routes/agent.py` | `POST /api/agent/ask`（JSON + trace） |
| `examples/agent_demo.py` | 无密钥演示：逐条撞击四道闸 + 完整链路 |
| `tests/test_agent_tools.py` | 17 条测试，锁定闸门与计算 |

### 9.1 实施中确定的两个补充决策

**（1）铁律不可被后台覆盖。** 管理员能改业务口径模板，但 `AGENT_RULES` 是代码常量，
拼接在模板之后。否则管理员改一次提示词就能删掉"不得自行乘除"，校验虽仍在，
但模型行为会立刻失控。落库版本号形如 `v3+agent-rules.1`，两者都可追溯。

**（2）换算公式按乘法口径实现，并修正了出成率的用法。**
PRD 原文「份数 ÷ 标准份分母 × 修正系数」与「1 份 = 0.15 kg 净料」自相矛盾。
实现取：`投料 kg = 份数 × 分母克数 ÷ 1000 ÷ 实际出成率`，向上取整到 0.1 kg；
`修正系数 = 实测 ÷ 基准` 只作偏离度指标，超过 ±20% 标 `needs_review`。
**这一条仍需业务书面确认**，否则 TDD 的基准用例会和 PRD 原文对不上。

### 9.2 已验证的行为（`python examples/agent_demo.py`）

| 场景 | 结果 |
|---|---|
| 伪造 `run_id` 直接建单 | `MISSING_PREREQUISITE` + 指回 `validate_material_need` |
| 把 49.0 改成 45 建单 | `PROVENANCE_MISMATCH`，回显 `kg = 49.0` |
| 跨会话复用凭证 | `SESSION_MISMATCH` |
| 缺效期批次去建单 | `SHELF_LIFE_BLOCKED` |
| 同一凭证建第二个草案 | `TOKEN_CONSUMED` |
| 调 `confirm_suggestion` 等 | `TOOL_UNKNOWN`（工具根本不存在） |
| 正常链路 | 校验 → 效期 → 草案，3 步 trace 全绿 |

### 9.3 未做（下一步）

- 流式版智能体入口（把最后一段回答按片推送，契约不变；工具循环仍走非流式）
- 前端把建议卡片按 `tool_result` 渲染（第四道防御，目前只有接口没有界面）
- T3 四个人工动作的界面按钮（`/prep/drafts/{id}`、`/prep/handovers/{id}`）
- 回写 outbox（`core/writeback.py` 仍是规格）

