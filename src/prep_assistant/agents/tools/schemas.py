"""8 个注册给模型的工具（OpenAI / DeepSeek 兼容的 function calling 描述）。

拆分原则见 docs/agent-tooling-design.md：
- T0 只读 3 个：无副作用，可反复调用
- T1 校验 2 个：纯函数 + 签发票据
- T2 草案 3 个：只写自有台账，状态恒为 draft
- T3 四个动作（确认/接手/回写/隔离）**不在此文件**——它们不进模型上下文

Schema 层的两个硬要求（第一道防御）：
- `additionalProperties: false`——模型不能塞计划外参数
- `required` 全覆盖——缺字段由框架直接拒，不给模型"自由发挥"的空间
"""

from __future__ import annotations

TOOL_SCHEMAS: list[dict] = [
    # ── T0 只读 ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "检索公司备料制度条文。回答制度、流程、口径类问题前必须先调用本工具，"
                "不得凭记忆作答。返回片段带 source 与相关度。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题原句",
                              "minLength": 2, "maxLength": 200},
                    "top_k": {"type": "integer", "description": "返回片段数，默认 3",
                              "minimum": 1, "maximum": 5},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_batch_status",
            "description": (
                "查询单个批次的解冻状态、剩余效期、实测出成率。批次号未知时先向用户确认，"
                "不得猜测或编造批次号。字段缺失时返回 null 并在 missing 中列出。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "batch_no": {"type": "string", "description": "批次号，如 B20261030-01",
                                 "pattern": "^[A-Za-z0-9_-]{4,32}$"},
                },
                "required": ["batch_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sync_status",
            "description": "查询各外部系统与字典/提示词的同步状态，判断数据是否可能过期。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    # ── T1 校验（签发凭证）──────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "validate_material_need",
            "description": (
                "校验六要素是否齐备并计算建议用量。任何涉及数量、重量、份数的问题都必须先调用本工具，"
                "禁止自行乘除、估算或换算单位。成功返回 run_id 与逐位可信的 kg；"
                "失败返回缺失要素，不返回任何数值。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_code": {"type": "string", "description": "内部品类编码，如 M-CHK-001"},
                    "portions": {"type": "integer", "description": "订货份数",
                                 "minimum": 1, "maximum": 20000},
                    "batch_no": {"type": "string", "description": "批次号"},
                },
                "required": ["item_code", "portions", "batch_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_shelf_life",
            "description": (
                "检查某批次在计划开工时点的剩余效期是否足够，返回 ok/warn/block 三级。"
                "涉及「这批还能不能用」「要不要报废」时必须先调用。"
                "你无权更改效期、无权放行食品、无权隔离批次。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "batch_no": {"type": "string"},
                    "planned_start_at": {"type": "string",
                                         "description": "计划开工时间，ISO 8601，如 2026-10-31T07:00:00+08:00"},
                },
                "required": ["batch_no", "planned_start_at"],
                "additionalProperties": False,
            },
        },
    },
    # ── T2 草案（写自有台账）────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_suggestion_draft",
            "description": (
                "在当前会话内创建备料建议草案。必须先取得本会话签发的 run_id 与 risk_id，"
                "且 kg 必须与校验返回的值完全一致。草案状态为 draft，"
                "需班组长本人在界面确认后才生效，你不能代其确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "pattern": "^run_[0-9a-f]{32}$"},
                    "risk_id": {"type": "string", "pattern": "^risk_[0-9a-f]{32}$"},
                    "item_code": {"type": "string"},
                    "kg": {"type": "number", "description": "必须与 validate_material_need 返回的 kg 一致",
                           "minimum": 0.1, "maximum": 10000},
                },
                "required": ["run_id", "risk_id", "item_code", "kg"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_handover_task",
            "description": (
                "创建交接待办。必须含业务编号、状态说明、未决事项、接手人。"
                "创建后状态为 pending_accept，对方确认接手才算完成，你不能代替对方确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "biz_no": {"type": "string", "description": "业务编号，如 PREP-20261031-0007"},
                    "biz_version": {"type": "string", "description": "单据版本号（可选）"},
                    "status_summary": {"type": "string", "maxLength": 200},
                    "open_items": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                    "to_employee_no": {"type": "string", "description": "接手人工号"},
                    "due_at": {"type": "string", "description": "期望接手时间，ISO 8601（可选）"},
                },
                "required": ["biz_no", "status_summary", "open_items", "to_employee_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_batch_anomaly",
            "description": (
                "上报批次异常（如解冻后促销取消、温度异常）。只登记与通知，"
                "不改变批次可用状态；隔离与放行只能由质量人员操作。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "batch_no": {"type": "string"},
                    "reason_code": {
                        "type": "string",
                        "enum": ["PROMO_CANCELLED", "THAW_TIMEOUT", "TEMP_DEVIATION",
                                 "QUALITY_SUSPECT", "OTHER"],
                    },
                    "note": {"type": "string", "maxLength": 300},
                },
                "required": ["batch_no", "reason_code"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]

# 只读工具：编排层允许在一轮内重复调用（例如先查两个批次）
READONLY_TOOLS = {"search_knowledge", "get_batch_status", "get_sync_status"}
