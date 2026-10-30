#!/usr/bin/env bash
# 构建：装依赖 → 静态检查 → 跑测试
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"

echo "==> 安装依赖"
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt

echo "==> 静态检查"
"$PYTHON" -m ruff check .

echo "==> 测试"
"$PYTHON" -m pytest -q

echo "==> 完成"
