"""F1 建议单生成（份数 → 公斤换算）。

公式（本实现的口径，与 PRD 原文的差异见下方「口径修正」）：

    净料需求 kg = 份数 × 标准份分母(g) ÷ 1000
    投料需求 kg = 净料需求 kg ÷ 实际出成率        # 出成率 < 1 时要多投料
    修正系数   = 实际出成率 ÷ 字典基准出成率      # 仅作偏离度指标，不参与乘除

口径修正（必须记住，否则算错两个数量级）：
- PRD 原文写「份数 ÷ 标准份分母 × 修正系数」，与同节「1 份 = 0.15 kg 净料」的定义
  自相矛盾——除法会得到"份/克"这种无意义量纲。本实现按乘法口径，即
  `份数 × 分母克数 ÷ 1000`，与 `examples/demo.py` 一致。
- 出成率的物理含义是「成品净重 ÷ 原料毛重」，因此投料量必须是 **除**，
  且除的是实际出成率；「修正系数」只是相对基准的偏离度，用来触发转人工。

约束（决策 B4 = 确定性规则，禁止模型）：
- 六要素缺一不出值：份数 / 分母 / 批次号 / 出成率 / 修正系数 / 结果
- 结果保留 1 位小数，向上取整到 0.1 kg（宁可多备一点，不可缺配）
- 每条建议必须能指回当时的字典版本，保证逐位可重建
- 计算过程不得读取系统当前时间以外的环境状态
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from prep_assistant.core.masterdata import Material

# 偏离度阈值：实测出成率相对基准超过 ±20% 时转人工（config/default.yaml yield.deviation_threshold）
DEVIATION_THRESHOLD = 0.20

# 六要素的字段名与中文标签，缺项时一并返回，便于模型如实转述
ELEMENT_LABELS = {
    "portions": "订货份数",
    "denominator_g": "标准份分母",
    "batch_no": "批次号",
    "yield_rate": "实际出成率",
    "correction_factor": "修正系数",
    "kg": "建议用量",
}


@dataclass(frozen=True)
class ConversionResult:
    """换算结果。ok=False 时 kg 必为 None——绝不返回"半成品数字"。"""

    ok: bool
    kg: float | None
    missing: list[str] = field(default_factory=list)
    missing_labels: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    basis: str = ""

    def __post_init__(self) -> None:
        if self.ok and self.kg is None:  # 不变式：ok=True 必须有值
            raise ValueError("换算成功时必须给出 kg")


def ceil_to_tenth(value: float) -> float:
    """向上取整到 0.1 kg（缺配代价远高于多备）。"""
    return math.ceil(round(value, 6) * 10) / 10


def net_kg(portions: int, denominator_g: int) -> float:
    """净料需求：份数 × 每份净料克数。"""
    return portions * denominator_g / 1000.0


def convert(
    material: Material | None,
    portions: int | None,
    batch_no: str | None,
    measured_yield: float | None,
) -> ConversionResult:
    """执行换算，六要素缺一即返回缺失清单。

    参数全为「已取出的事实」，本函数不查库、不读时钟、不调外部系统，
    因此可被逐位重放（f45）。
    """
    missing: list[str] = []

    if not portions or portions <= 0:
        missing.append("portions")
    if material is None or not material.denominator_g:
        missing.append("denominator_g")
    if not batch_no:
        missing.append("batch_no")
    if measured_yield is None:
        missing.append("yield_rate")

    if missing:
        return ConversionResult(
            ok=False,
            kg=None,
            missing=missing,
            missing_labels=[ELEMENT_LABELS[m] for m in missing],
            detail={"portions": portions, "batch_no": batch_no,
                    "measured_yield": measured_yield},
        )

    assert material is not None and portions is not None and measured_yield is not None

    net = net_kg(portions, material.denominator_g)
    # 出成率异常保护：<=0 或 >1 都视为脏数据，不出值（宁可空白，不可瞎算）
    if not (0 < measured_yield <= 1):
        return ConversionResult(
            ok=False,
            kg=None,
            missing=["yield_rate"],
            missing_labels=["实际出成率（取值异常，需人工核对）"],
            detail={"measured_yield": measured_yield},
        )

    raw = net / measured_yield
    kg = ceil_to_tenth(raw)
    factor = round(measured_yield / material.base_yield, 4)
    deviation = abs(1 - factor)
    needs_review = deviation > DEVIATION_THRESHOLD

    return ConversionResult(
        ok=True,
        kg=kg,
        detail={
            "portions": portions,
            "denominator_g": material.denominator_g,
            "net_kg": round(net, 3),
            "yield_rate": measured_yield,
            "base_yield": material.base_yield,
            "correction_factor": factor,
            "deviation": round(deviation, 4),
            "needs_review": needs_review,
            "rounded_from": round(raw, 3),
        },
        basis=(
            f"净料 {net:.3f}kg ÷ 实测出成率 {measured_yield:.2f} = {raw:.3f}kg，"
            f"向上取整至 0.1kg 得 {kg}kg；修正系数 {factor:.2f}"
            f"（{'偏离基准超 20%，需人工复核' if needs_review else '在基准范围内'}）"
        ),
    )
