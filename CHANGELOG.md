# 更新记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- **Agent Harness 执行循环（阶段 4b）**：模型决策 → 工具执行 → 结果回填 → 再决策，
  上限 **5 轮**；轮次日志表 `agent_round_logs`（工具名/参数/耗时/结果摘要，供阶段 5 分析）；
  `create_suggestion_draft` 执行前**强制中断**——预校验通过即挂起 `agent_pending_actions`、
  草案预览流式推给员工，确认（`POST /api/agent/draft/{id}/confirm`）后才真正执行，
  拒绝不留单据；错误码 `ACTION_NOT_FOUND` / `ACTION_STATE_CONFLICT` / `ACTION_EXPIRED`
- `POST /api/agent/ask` 改为 SSE 流式（事件序 `meta → round* → tool* → draft_preview? → answer* → done`）
- `examples/agent_demo.py` 第二段改为演示中断→确认全流程与轮次日志
- 轨迹示例文档 `docs/agent-loop.md`（正常建单 / 要素缺失自愈 / 越权拦截）
- **智能体工具层（阶段 4）**：8 个 function calling 工具（T0 只读 / T1 校验签凭证 / T2 草案），
  确认、接手、回写、隔离四个动作不注册给模型；凭证链四道闸（HMAC + 会话归属 + 10 分钟 TTL +
  一次性消费 + 参数哈希逐位比对）；自写薄编排循环（预算 6 次调用 + 全链路 trace）
- `POST /api/agent/ask` 自主决策入口（非流式，返回 answer + traces）
- `core/conversion.py`（F1 换算 + 六要素闸门）、`core/shelf_life.py`（F3 三级效期判定）**已实现**
- `config/dict.yaml` 版本化字典（3 个品类 + 效期参数）
- `examples/agent_demo.py`：无密钥可跑的闸门演示
- `docs/agent-tooling-design.md`：工具层设计稿 + 实施记录
- 测试 47 条（新增 17 条：闸门拒绝、换算口径、预算收口、铁律不可覆盖）
- 仓库结构标准化：`src/` 布局、`tests/`、`examples/`、`scripts/`、`config/`、`.github/`
- 需求基线 `docs/备料助手-一页纸需求清单.md`（v1.0）
- 决策记录 `docs/decision-log.md`（B1–B6，11 项决策）
- CI：推送与 PR 时自动跑测试与静态检查

### Changed
- 工具铁律（`AGENT_RULES`）改为**代码内置、后台不可编辑**，与管理员可改的业务模板拼接
- `README.md` 重写为可上手版本
- `docs/prd-agent-assistant-capability-a.md` 降级为参考材料：「只读不回写」等结论已被决策推翻

### Known issues
- 换算公式按乘法口径实现，与 PRD 原文措辞冲突，需业务书面确认（见设计文档 §9.1）
- F2/F4–F11 仍为规格；回写 outbox 未实现；建议卡片前端渲染未做

## [0.1.0] - 2026-10-23

### Added
- 项目初始化：业务发现报告、流程图、试点策划报告
