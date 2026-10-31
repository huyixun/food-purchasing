"""F3 短保风险提示。

触发来源：
- 效期倒排：剩余效期不足以覆盖加工与配送窗口
- 场景拦截：促销临时取消且原料已解冻，库存不可视为可长期保存

约束：
- 只提示、不改配方、不放行食品（红线）
- 提示必须标注触发原因、接手人与确认时间，不得只给一句模糊提醒
- 触发后该行不自动进入可确认状态，须人工判断并签核

判定是纯算术 + 阈值比较，没有任何模型参与，因此可逐位重放（f45）。
三级语义：
    ok    —— 剩余效期覆盖「加工时长 + 安全缓冲」
    warn  —— 覆盖不足但仍在缓冲比例之上，需人工评估，不自动放行
    block —— 明显不足，禁止据此建草案；不得延长效期，只能换批次或转质量人员
"""

from __future__ import annotations

from dataclasses import dataclass, field

REASON_LABELS = {
    "SHELF_LIFE_INSUFFICIENT": "剩余效期不足以覆盖加工与缓冲窗口",
    "SHELF_LIFE_MISSING": "该批次缺少剩余效期数据",
    "PROMO_CANCELLED_AFTER_THAW": "促销取消但原料已解冻，不可视为可长期保存",
}


@dataclass(frozen=True)
class ShelfLifeResult:
    ok: bool
    level: str                                   # ok / warn / block
    remaining_hours: float | None
    required_hours: float | None
    process_hours: float | None
    buffer_hours: float | None
    reason_codes: list[str] = field(default_factory=list)
    reason_labels: list[str] = field(default_factory=list)
    evidence: str = ""
    advisory: str = ""

    @property
    def blocked(self) -> bool:
        return self.level == "block"


def check(
    remaining_hours: float | None,
    process_hours: float,
    buffer_hours: float,
    warn_ratio: float,
    thaw_state: str | None = None,
    promo_cancelled: bool = False,
) -> ShelfLifeResult:
    """判定某批次在计划开工时点是否可用。

    remaining_hours 为 None 时**不给 ok**——缺数据不等于安全，
    按 block 处理并标明原因是缺数据（f02：所有短保批次必须有状态）。
    """
    required = round(process_hours + buffer_hours, 2)

    # 场景拦截优先：已解冻 + 促销取消，无论剩余效期多长都要提示
    if promo_cancelled and thaw_state == "已解冻":
        return ShelfLifeResult(
            ok=True,
            level="warn",
            remaining_hours=remaining_hours,
            required_hours=required,
            process_hours=process_hours,
            buffer_hours=buffer_hours,
            reason_codes=["PROMO_CANCELLED_AFTER_THAW"],
            reason_labels=[REASON_LABELS["PROMO_CANCELLED_AFTER_THAW"]],
            evidence=f"批次已于 {thaw_state} 状态，促销已取消；剩余效期 {remaining_hours}h",
            advisory="不得延长效期。请班组长评估转其他菜品或由质量人员判定处置方式。",
        )

    if remaining_hours is None:
        return ShelfLifeResult(
            ok=False,
            level="block",
            remaining_hours=None,
            required_hours=required,
            process_hours=process_hours,
            buffer_hours=buffer_hours,
            reason_codes=["SHELF_LIFE_MISSING"],
            reason_labels=[REASON_LABELS["SHELF_LIFE_MISSING"]],
            evidence="批次台账中该批次剩余效期为空",
            advisory="缺数据不得放行。请先在备料工位扫码补录效期，或改用有状态的批次。",
        )

    if remaining_hours >= required:
        return ShelfLifeResult(
            ok=True,
            level="ok",
            remaining_hours=remaining_hours,
            required_hours=required,
            process_hours=process_hours,
            buffer_hours=buffer_hours,
            evidence=f"剩余 {remaining_hours}h ≥ 需求 {required}h（加工 {process_hours}h + 缓冲 {buffer_hours}h）",
            advisory="效期充足，可按流程备料。",
        )

    if remaining_hours >= required * warn_ratio:
        return ShelfLifeResult(
            ok=True,
            level="warn",
            remaining_hours=remaining_hours,
            required_hours=required,
            process_hours=process_hours,
            buffer_hours=buffer_hours,
            reason_codes=["SHELF_LIFE_INSUFFICIENT"],
            reason_labels=[REASON_LABELS["SHELF_LIFE_INSUFFICIENT"]],
            evidence=f"剩余 {remaining_hours}h 低于需求 {required}h，但仍在缓冲比例内",
            advisory="需班组长评估：优先排产、改用其他批次，不得延长效期。",
        )

    return ShelfLifeResult(
        ok=False,
        level="block",
        remaining_hours=remaining_hours,
        required_hours=required,
        process_hours=process_hours,
        buffer_hours=buffer_hours,
        reason_codes=["SHELF_LIFE_INSUFFICIENT"],
        reason_labels=[REASON_LABELS["SHELF_LIFE_INSUFFICIENT"]],
        evidence=f"剩余 {remaining_hours}h 远低于需求 {required}h",
        advisory="不得延长效期、不得放行。请改用其他批次，或由质量人员判定处置。",
    )
