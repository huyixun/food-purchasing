"""★② 切片器（自 examples/rag_prototype.py 抽取，逻辑未变）。

切分优先级：标题边界 > 段落边界 > 字数硬切。
不同主题绝不混进同一段，保证检索召回的片段语义完整。
"""

from __future__ import annotations

CHUNKER_VERSION = "v2"  # 切片算法变更时递增，旧缓存自动失效


def chunk_markdown(text: str, size: int = 500) -> list[str]:
    """把 markdown 切成约 size 字的片段（中文按字符计）。"""
    sections: list[str] = []
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#") and lines:  # 遇到新标题：前一个主题块封口
            sections.append("\n".join(lines))
            lines = []
        lines.append(line)
    if lines:
        sections.append("\n".join(lines))

    chunks: list[str] = []
    for section in sections:
        paras = [p.strip() for p in section.split("\n\n") if p.strip()]
        local = ""
        for para in paras:
            if len(para) > size:  # 单段超长：先落盘缓冲，再按 size 硬切该段
                if local:
                    chunks.append(local.strip())
                    local = ""
                chunks.extend(para[i:i + size] for i in range(0, len(para), size))
                continue
            if len(local) + len(para) + 1 <= size:
                local = f"{local}\n{para}" if local else para
            else:
                if local:
                    chunks.append(local.strip())
                local = para
        if local.strip():
            chunks.append(local.strip())
    return chunks
