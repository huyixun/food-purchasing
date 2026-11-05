# 备料助手（Prep Assistant）

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-62%20passed-brightgreen)
![AgentTools](https://img.shields.io/badge/agent%20tools-8-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

给中央厨房备料段的协同系统：把门店报的**成品份数**按**批次实测出成率**换算成**原料公斤数**，把短保批次的**解冻状态与剩余效期**变成可查状态，把跨岗交接变成「**对方确认接手才算完成**」的待办。

一句话概括设计取向：**模型负责串流程，数字永远由确定性规则算，写操作永远等人点**。

> **当前状态：可运行、可测试、未上线。**
> 四层系统（制度 RAG → 工具层 → 编排 Harness → 管理后台）均已实现，`pytest` 62 条全绿。
> 按决策记录 B6「本阶段不做现场上线」，仍缺确认卡片页与回写 outbox（见 [路线图](#路线图)），请勿用于生产。

## 这个系统不做什么

这几条是食安红线，**任何授权下都不开放**，也不提供关闭开关（详见 [`docs/decision-log.md`](docs/decision-log.md)）：

- 不自主下单、不延长效期、不替换配方
- 不由模型修改效期或放行食品，异常批次只能由质量人员隔离
- 不覆盖原记录——所有更正只能追加
- 不由模型替员工确认、接手、回写、隔离——这四类动作**根本不注册为工具**

## 安装

```bash
python -m venv .venv
source .venv/Scripts/activate     # Windows Git Bash；PowerShell 用 .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

## 运行

```bash
# 查看命令行入口与当前规格摘要
python -m prep_assistant.main --help

# 起服务：聊天页 http://127.0.0.1:8000/ ，管理后台 /admin/login
python -m prep_assistant.main serve

# 智能体工具层演示（不需要 API 密钥，直接看四道闸怎么拦人）
python examples/agent_demo.py

# 跑测试
pytest -q
```

## 文档

| 文档 | 内容 |
|---|---|
| `docs/备料助手-一页纸需求清单.md` | 需求基线（v1.0，11 项决策冻结） |
| `docs/decision-log.md` | 决策记录与依据（B1–B6） |
| `docs/design.md` | 架构分层与关键约束 |
| `docs/agent-tooling-design.md` | 智能体工具层：8 个工具、凭证链、系统提示词铁律 |
| `docs/api.md` | 接口说明（阶段 2/3/4 已实现） |
| `docs/discovery-report.md` | 业务发现报告（36 条访谈事实） |
| `docs/prd-agent-assistant-capability-a.md` | 早期 PRD（**仅作参考**，部分结论已被决策推翻） |

## 目录结构

```
.
├── src/prep_assistant/   # 源码：main 入口、core 业务、utils 工具
├── tests/                # 测试，结构与 src 对应
├── docs/                 # 需求、设计、接口、访谈材料
├── examples/             # 可运行示例
├── scripts/              # 构建与部署脚本
├── config/               # 非机密默认配置
├── data/                 # 样例数据（小文件）
└── .github/              # CI、Issue / PR 模板
```

## 许可证

MIT，见 `LICENSE`。若不打算开源，删除该文件并在 `README` 注明「内部项目，保留所有权利」。
