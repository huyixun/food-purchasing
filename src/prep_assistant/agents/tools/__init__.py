"""工具层对外出口。

只暴露四样东西：
- TOOL_SCHEMAS：给模型的 8 个工具定义（T0/T1/T2）
- execute：统一执行入口（错误结构化，不抛栈给模型）
- ToolContext：一次对话的执行上下文
- ToolError：可被上层捕获的类型化错误

T3 的四个动作（确认 / 接手 / 回写 / 隔离批次）**有意不在此导出**——
它们属于人类，不属于模型。
"""

from prep_assistant.agents.tools.errors import ToolError
from prep_assistant.agents.tools.impl import HANDLERS, ToolContext, execute
from prep_assistant.agents.tools.schemas import TOOL_NAMES, TOOL_SCHEMAS

__all__ = [
    "HANDLERS",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolError",
    "execute",
]
