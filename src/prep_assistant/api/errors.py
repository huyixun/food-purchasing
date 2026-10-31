"""统一错误契约。

约束（用户要求）：所有接口的错误必须返回错误码 + 中文错误信息，不暴露堆栈。
- 非流式接口：HTTP 状态码 + {"code", "message"}
- 流式接口（进入 SSE 之后才出的错）：event: error 事件，同样结构
"""

from __future__ import annotations

import json

# code -> (HTTP 状态码, 默认中文信息)
ERRORS: dict[str, tuple[int, str]] = {
    "INVALID_REQUEST": (422, "请求参数不合法"),
    "INVALID_QUESTION": (422, "问题不能为空，且不超过 500 字"),
    "SESSION_NOT_FOUND": (404, "会话不存在或已过期"),
    "RATE_LIMITED": (429, "提问太频繁，请稍后再试"),
    "LLM_UNAVAILABLE": (503, "大模型服务暂不可用，请稍后重试或联系管理员"),
    "LLM_TIMEOUT": (504, "生成超时，请稍后重试"),
    "INTERNAL_ERROR": (500, "服务内部错误，请联系管理员"),
    # ── 管理后台 ──
    "UNAUTHORIZED": (401, "登录已失效，请重新登录"),
    "FORBIDDEN": (403, "没有权限执行此操作"),
    "INVALID_CREDENTIALS": (401, "工号或密码不正确"),
    "UNSUPPORTED_FILE": (422, "仅支持 .md / .markdown / .pdf 文件"),
    "FILE_TOO_LARGE": (422, "文件超过 5 MB 上限"),
    "FILE_EMPTY": (422, "文件内容为空，或 PDF 中未提取到文字（扫描件需要 OCR，本期不支持）"),
    "LAST_SOURCE": (400, "至少保留一个知识来源，不能删除最后一个"),
    # ── 智能体确认流（create_suggestion_draft 强制中断）──
    "ACTION_NOT_FOUND": (404, "确认请求不存在或已失效"),
    "ACTION_STATE_CONFLICT": (409, "该请求已处理过，请勿重复操作"),
    "ACTION_EXPIRED": (410, "确认已超时（10 分钟），请重新发起"),
}


class AppError(Exception):
    """业务异常：路由层抛出，由 FastAPI 异常处理器统一转成错误体。"""

    def __init__(self, code: str, message: str | None = None):
        if code not in ERRORS:
            raise ValueError(f"未知错误码：{code}")
        self.code = code
        self.status, default_message = ERRORS[code]
        self.message = message or default_message
        super().__init__(self.message)


def error_body(code: str, message: str | None = None) -> dict:
    """构造标准错误体。未知 code 一律收敛为 INTERNAL_ERROR，防止泄漏内部细节。"""
    if code not in ERRORS:
        code = "INTERNAL_ERROR"
    return {
        "code": code,
        "message": message or ERRORS[code][1],
    }


def sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件（事件名 + 单行 JSON + 空行）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
