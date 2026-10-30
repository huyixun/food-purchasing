"""脚手架自检测试。

真实业务测试随实施计划逐条补充，遵循「先写失败测试再实现」。
本文件只验证包结构、入口与规格文件就位，不依赖第三方库。
"""

from __future__ import annotations

import importlib
import io
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CORE_MODULES = [
    "masterdata",
    "batch",
    "conversion",
    "shelf_life",
    "confirmation",
    "handover",
    "audit",
    "writeback",
    "health",
    "replay",
]


def test_version() -> None:
    from prep_assistant import __version__

    assert __version__ == "0.1.0"


def test_core_modules_importable() -> None:
    for name in CORE_MODULES:
        module = importlib.import_module(f"prep_assistant.core.{name}")
        assert module.__doc__, f"core.{name} 缺少规格说明"


def test_main_smoke() -> None:
    from prep_assistant.main import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main([])

    assert code == 0
    out = buf.getvalue()
    assert "prep-assistant" in out
    assert "红线" in out


def test_red_lines_declared() -> None:
    from prep_assistant.main import RED_LINES

    assert len(RED_LINES) == 4
    assert any("配方" in line for line in RED_LINES)


def test_spec_files_exist() -> None:
    for relative in ("docs/备料助手-一页纸需求清单.md", "docs/decision-log.md"):
        assert (REPO_ROOT / relative).exists(), f"缺少规格文件：{relative}"


def test_default_config_exists() -> None:
    assert (REPO_ROOT / "config" / "default.yaml").exists()
