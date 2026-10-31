"""工具层统一错误：每个错误都必须能"把模型拉回正轨"。

设计依据 docs/agent-tooling-design.md §3.3：
模型被拒后最容易干的事就是编一个数糊过去，所以错误里必须带
`missing_step`（该先调哪个工具）和 `recover_hint`（怎么改），
而不是只丢一个 422。
"""

from __future__ import annotations

# code -> (HTTP 语义码, 默认中文信息, 该先完成哪一步)
ERRORS: dict[str, tuple[int, str, str]] = {
    "MISSING_PREREQUISITE": (422, "缺少前置校验凭证，不能创建草案", "validate_material_need"),
    "PROVENANCE_MISMATCH": (409, "提交的内容与校验结果不一致，或凭证被篡改", "validate_material_need"),
    "TOKEN_EXPIRED": (410, "校验凭证已过期（有效期 10 分钟），请重新校验", "validate_material_need"),
    "TOKEN_CONSUMED": (409, "该凭证已用于创建草案，不能重复使用", "validate_material_need"),
    "SESSION_MISMATCH": (403, "凭证不属于当前会话，不能跨会话使用", "validate_material_need"),
    "SHELF_LIFE_BLOCKED": (409, "效期判定为不可用，禁止创建草案", "check_shelf_life"),
    "MISSING_FIELDS": (422, "必填要素不完整", ""),
    "NOT_FOUND": (404, "未找到对应记录", ""),
    "TOOL_UNKNOWN": (400, "不存在该工具", ""),
    "CALL_LIMIT_EXCEEDED": (429, "单轮工具调用次数已达上限", ""),
}


class ToolError(Exception):
    """工具执行失败。to_dict() 的产物直接作为 tool message 回给模型。"""

    def __init__(self, code: str, message: str | None = None, **extra) -> None:
        status, default_message, step = ERRORS.get(code, (500, "工具执行失败", ""))
        self.code = code
        self.status = status
        self.message = message or default_message
        self.missing_step = step
        self.extra = extra
        super().__init__(self.message)

    def to_dict(self) -> dict:
        body: dict = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.missing_step:
            body["missing_step"] = self.missing_step
        body.update(self.extra)
        return body
