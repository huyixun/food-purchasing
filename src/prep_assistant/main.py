"""程序入口。

子命令：
    （默认）        规格自检：打印版本、配置位置与已冻结的硬约束
    serve           启动员工问答服务（FastAPI + SSE 聊天页）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

# 决策与访谈共同确定、不可通过配置关闭的红线
RED_LINES = (
    "不自主下单、不延长效期、不替换配方",
    "不由模型修改效期或放行食品，异常批次只能由质量人员隔离",
    "不覆盖原记录，所有更正只能追加",
    "数值链路为确定性规则，禁止引入模型",
)

SPEC_FILES = (
    "docs/备料助手-一页纸需求清单.md",
    "docs/decision-log.md",
)


def _repo_root() -> Path:
    """返回仓库根目录（src 布局下向上三级）。"""
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prep-assistant",
        description="中央厨房备料助手",
    )
    parser.add_argument("--version", action="version", version=f"prep-assistant {__version__}")
    parser.add_argument(
        "--config",
        default="config/prototype.yaml",
        help="配置文件路径（相对仓库根目录）",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["info", "serve"],
        default="info",
        help="info=规格自检（默认）；serve=启动问答服务",
    )
    parser.add_argument("--host", default="127.0.0.1", help="serve 模式监听地址")
    parser.add_argument("--port", type=int, default=8000, help="serve 模式监听端口")
    return parser


def _print_info(args: argparse.Namespace) -> int:
    root = _repo_root()
    config_path = root / args.config

    print(f"prep-assistant {__version__}")
    print(f"仓库根目录: {root}")
    print(f"配置: {config_path}  [{'存在' if config_path.exists() else '缺失'}]")
    print("规格文件:")
    for item in SPEC_FILES:
        print(f"  - {item}  [{'存在' if (root / item).exists() else '缺失'}]")
    print("红线（不可配置关闭）:")
    for line in RED_LINES:
        print(f"  · {line}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    """启动问答服务。uvicorn 延迟导入：info 模式无需安装 Web 依赖。"""
    import uvicorn

    from prep_assistant.api.app import app  # noqa: F401 应用装配（含建表与种子账号）

    print(f"prep-assistant {__version__} · 员工问答入口 http://{args.host}:{args.port}/")
    print(f"prep-assistant {__version__} · 管理后台     http://{args.host}:{args.port}/admin/")
    print("（首次启动已生成 admin 随机密码，见控制台输出或 data/.admin_credentials）")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    return _print_info(args)


if __name__ == "__main__":
    sys.exit(main())
