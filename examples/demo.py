"""示例：一条建议单应该长什么样。

占位示例——尚未接入 core.conversion，数值为手工填写，仅用于对齐评审时的呈现口径。
运行：python examples/demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prep_assistant import __version__  # noqa: E402

# 六要素缺一不可：份数 / 单位净料 / 批次号 / 出成率 / 修正系数 / 结果
# 算法：份数 × 单位净料(kg) × 修正系数，向上取整到 0.1 kg
# 注意：PRD 原文写作「份数 ÷ 标准份分母」，与其「1 份 = 0.15 kg 净料」的定义自相矛盾，
#       此处按乘法口径，待实施计划阶段与业务统一措辞。
ROWS = [
    ("宫保鸡丁", 300, 0.15, "B20261030-01", 0.92, 0.95, 42.8, "已同步"),
    ("番茄炒蛋", 500, 0.20, "B20261030-02", 0.88, 1.00, 100.0, "待同步"),
    ("清炒时蔬", 400, 0.12, "B20261030-03", 0.00, 1.00, None, "不出建议：缺出成率"),
]


def main() -> None:
    print(f"prep-assistant {__version__} · 建议单示例（占位数据）\n")
    header = f"{'品类':<10}{'份数':>6}{'单位净料':>10}{'修正系数':>9}{'建议kg':>9}  状态"
    print(header)
    print("-" * len(header) + "-" * 12)
    for name, portions, denom, batch, rate, factor, result, status in ROWS:
        shown = "—" if result is None else f"{result:.1f}"
        print(f"{name:<10}{portions:>6}{denom:>10.2f}{factor:>9.2f}{shown:>9}  {status}")
    print("\n注意：六要素不全的行不得出现建议值（此处第三行即为例外情形）。")


if __name__ == "__main__":
    main()
