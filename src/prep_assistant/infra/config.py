"""配置加载（自 examples/rag_prototype.py 抽取）。

管理员只改 config/prototype.yaml 即可切换供应商与检索参数；
密钥一律走环境变量，禁止写进配置文件。
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]  # infra → prep_assistant → src → 仓库根
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "prototype.yaml"

_REQUIRED_SECTIONS = ("llm", "retrieval", "source", "cache_dir")


def load_config(path: str | Path | None = None) -> dict:
    """读取 YAML 配置；缺少必需段时直接报错，不让错误流到请求处理中途。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in _REQUIRED_SECTIONS:
        if key not in cfg:
            raise KeyError(f"配置缺少必需段：{key}")
    return cfg
