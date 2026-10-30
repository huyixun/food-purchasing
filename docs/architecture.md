# 技术架构方案（v0.1 待评审）

> 对象：备料助手智能体系统。约束来自 `docs/decision-log.md`（B1–B6）。
> 本方案不写代码，只定结构与边界。

## 0. 一个总原则

**LLM 不产生业务数据。** 它只做三件事：理解意图、检索知识、生成解释。
任何带数量的动作（换算、备多少、还剩几天）必须经确定性服务层计算并落库；
LLM 的输出只是「建议的下一步动作 + 参数草案」，由服务层校验后执行。

## 1. 四层架构

```
┌─────────────────────────────────────────────────────────┐
│ 入口层  FastAPI(异步) + Web/平板                          │
│  认证(具名身份) · 会话 · 审计中间件 · 限流                  │
├─────────────────────────────────────────────────────────┤
│ 智能体编排层（可选路径，旁路于业务主流程）                    │
│  意图路由 → RAG检索 → 工具调用(只读优先) → 护栏 → 回答       │
│  LLM 网关(可换供应商) · 提示词版本管理 · 引用溯源            │
├─────────────────────────────────────────────────────────┤
│ 服务层  确定性业务能力（core F1–F11，无 LLM）               │
│  换算 · 批次状态机 · 交接 · 回写(outbox) · 留痕             │
├─────────────────────────────────────────────────────────┤
│ 数据模型层  PostgreSQL(+pgvector) · SQLAlchemy/alembic    │
│  业务表 · 知识库表 · 会话表 · outbox · 审计 · 向量索引       │
└─────────────────────────────────────────────────────────┘
```

层级规则：

1. **入口层不写业务逻辑**，只做认证、参数校验、调用服务层或编排层。
2. **编排层旁路于业务主流程**：员工可以直接用界面确认建议单（不经过 LLM），
   也可以提问（经过 LLM）。LLM 挂了，备料流程照常——这是 f35「现有人工流程继续运行」的架构表达。
3. **服务层是唯一有权写业务表的地方**；编排层只能调用它，且工具以只读为主，
   写操作只产「草案」，由人工确认后落库（对齐 B1.2 两级授权）。
4. **知识条目分两级**：公开问答级 / 商业秘密级（配方、出成率规则）。
   秘密级不进任何 LLM 上下文，只在服务端被确定性代码引用。

## 2. 目录结构（tree）

```
src/prep_assistant/
├── main.py                    # 应用装配
├── api/                       # ── 入口层 ──
│   ├── deps.py                # 认证 / 会话 / 权限依赖
│   ├── middleware.py          # 审计日志、限流、请求 ID
│   └── routes/
│       ├── chat.py            # 员工：问答（流式）
│       ├── tasks.py           # 员工：发起（备料/交接/异常上报）
│       ├── knowledge.py       # 管理员：知识库维护
│       ├── records.py         # 管理员：记录查询与导出
│       └── batches.py         # 员工：批次扫码与状态流转
├── agents/                    # ── 智能体编排层 ──
│   ├── router.py              # 意图识别 → 路由（问答/发起/查数/闲聊拒绝）
│   ├── guardrails.py          # 数值类问题强制转服务层；回答必须带引用
│   ├── llm/
│   │   ├── gateway.py         # 供应商无关接口（超时/重试/降级）
│   │   ├── providers/
│   │   │   ├── base.py        # 抽象基类
│   │   │   ├── openai_compat.py  # OpenAI 兼容协议（多数国产模型可用）
│   │   │   └── local.py       # 私有化部署模型
│   │   └── prompts/           # 提示词按版本号管理，入库可追溯
│   ├── rag/
│   │   ├── chunker.py         # 知识条目切分
│   │   ├── indexer.py         # 嵌入 + 写入向量索引（可跳过秘密级）
│   │   └── retriever.py       # top-k 检索，返回条目+版本
│   └── tools/                 # 暴露给 LLM 的工具，只读为主
│       ├── lookup_knowledge.py
│       ├── query_batch.py     # 只读查批次状态
│       └── draft_task.py      # 只产草案，不直接写业务表
├── core/                      # ── 服务层（已有 F1–F11，不变） ──
│   ├── conversion.py  batch.py  shelf_life.py  confirmation.py
│   ├── handover.py    audit.py  writeback.py   health.py  replay.py
│   └── masterdata.py
├── knowledge/                 # 知识库领域（新增，与 core 平级）
│   ├── models.py              # 菜品-原料 BOM、保质期、制度条目
│   ├── service.py             # 版本签发、生效日期、秘密级标记
│   └── versioning.py          # 只增不改，对齐口径字典模式
├── infra/
│   ├── db.py                  # async engine / session
│   ├── outbox.py              # 回写投递 worker
│   └── config.py              # pydantic-settings，读 config/*.yaml
└── utils/
    ├── clock.py               # 服务端时间 / 双时态
    └── ids.py                 # 业务编号 / 幂等键
```

## 3. 数据库表设计

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | id, employee_no(唯一), name, role, is_active | 具名身份，禁共用账号 |
| `knowledge_items` | id, category(dish/ingredient/policy), title, content, **version**, status(draft/published/retired), effective_date, is_secret, created_by | 知识条目，只增不改；`is_secret=true` 不进 LLM |
| `knowledge_relations` | id, dish_id, ingredient_id, qty_per_portion, unit, version | 菜品→原料 BOM，随条目版本走 |
| `knowledge_embeddings` | item_id, version, chunk_text, embedding vector(1024) | pgvector；秘密级条目不写此表 |
| `batches` | id, batch_no, category_id, thaw_status, remaining_shelf_days, actual_yield, **version** | F2 批次状态机，助手为权威源 |
| `suggestions` | id, biz_no(唯一), business_date, dictionary_version, status, total_kg | F1 建议单 |
| `suggestion_rows` | suggestion_id, portions, unit_kg, batch_id, yield_rate, factor, result_kg, six_element_complete | 六要素缺一不出值 |
| `handovers` | id, biz_no, current_version, status, open_items, receiver_id, accepted_at | F5 五要素；accepted_at 为空=未完成 |
| `task_drafts` | id, conversation_id, type, payload jsonb, status(draft/confirmed), created_by | LLM 产的草案，人工确认后才成单 |
| `conversations` | id, user_id, created_at | 会话 |
| `messages` | id, conversation_id, role, content, citations jsonb, provider, prompt_version, latency_ms | 回答必须带 citations |
| `llm_calls` | id, provider, model, prompt_version, tokens_in/out, latency_ms, redacted, created_at | 成本与审计，可按版本归因 |
| `audit_logs` | id, biz_no, reason, actor_id, server_time, conclusion, row_version | 四段式，append-only，双时态 |
| `outbox_events` | id, idempotency_key(唯一), target, payload, status, retries, next_retry_at | F7 回写投递 |
| `write_conflicts` | id, event_id, local_value, remote_value, remote_version, resolver_id, resolved_at | F8 冲突裁决队列 |

索引要点：`suggestion_rows(suggestion_id)`、`batches(category_id, thaw_status)`、
`outbox_events(status, next_retry_at)`、`messages(conversation_id)`、
`audit_logs(biz_no)`；向量检索用 pgvector HNSW。

## 4. 第一周最小验证目标（Walking Skeleton）

目标不是做功能，是**打通一条纵切面**，证明四个假设成立：

| # | 验证假设 | 做法 | 通过标准 |
|---|---|---|---|
| 1 | 链路能跑 | docker compose 起 PG+pgvector；FastAPI 起 `/chat`、`/tasks` | curl 一问一答成功 |
| 2 | 回答可溯源 | 手工录入 5 条菜品知识（原料+保质期），检索 top-3 回答 | 每个回答都带「条目+版本」引用 |
| 3 | LLM 可替换 | 网关接一个 OpenAI 兼容供应商，改配置切到第二个 | 重启后同一问题仍可回答，代码零改动 |
| 4 | 红线生效 | 问「这批鸡胸肉今天备多少公斤」 | 不走 LLM 算数：路由到服务层并返回「换算功能未上线」，同时落一条路由日志 |
| 5 | 动作有痕 | 发起一条任务草案 → 人工确认 → 落库 | `task_drafts` + `audit_logs` 各一条，可按 biz_no 查到 |
| 6 | 环境可复现 | `scripts/build.sh` 一键起环境跑测试 | 新人 clone 后 10 分钟内跑通 |

**明确不在第一周做**：换算计算（F1）、回写（F7/F8）、批量导入知识、
向量检索调优、权限细粒度（先只有员工/管理员两档）。

## 5. 待评审的开放点

| # | 事项 | 我的倾向 |
|---|---|---|
| 1 | 向量库用 pgvector 还是独立向量库 | pgvector——私有化部署少一个组件 |
| 2 | 嵌入模型本地还是 API | 本地（bge 类），避免知识出企业边界 |
| 3 | 编排层用框架（LangChain 类）还是自写 | 自写薄编排——需求只有路由+RAG+工具，框架的抽象税不划算 |
| 4 | 会话记忆存储 | 先落 `messages` 表，不做跨会话记忆（合规更简单） |
