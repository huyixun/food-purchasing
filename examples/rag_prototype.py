"""阶段 1 原型 CLI：裸问答（命令行 RAG）。

阶段 2 起核心逻辑已抽取到正式模块，本文件只保留命令行外壳：
    ★① LLM 网关   → src/prep_assistant/agents/llm/gateway.py
    ★② 切片器     → src/prep_assistant/agents/rag/chunker.py
    ★③ 向量化器   → src/prep_assistant/agents/rag/indexer.py
    ★④ 混合检索器 → src/prep_assistant/agents/rag/retriever.py
    ★⑤ 提示词     → src/prep_assistant/agents/llm/prompts.py

运行：
    python examples/rag_prototype.py --ask "短保原料怎么处理" --no-llm
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prep_assistant.agents.llm.gateway import LLMConfigError, call_llm  # noqa: E402
from prep_assistant.agents.llm.prompts import build_messages  # noqa: E402
from prep_assistant.agents.rag.indexer import build_dense_index, build_or_load_index  # noqa: E402
from prep_assistant.agents.rag.retriever import hybrid_search  # noqa: E402
from prep_assistant.infra.config import load_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def answer(question: str, chunks, vectors, dense_index, cfg: dict, use_llm: bool = True) -> None:
    """一次完整的问答回合：检索 → 展示证据 → （可选）调 LLM。"""
    hits = hybrid_search(question, chunks, vectors, dense_index, cfg)
    print("\n" + "─" * 12 + f" 检索 top{len(hits)} " + "─" * 12)
    for i, h in enumerate(hits, 1):
        print(f"[资料{i}] 融合={h['score']}  向量={h['dense']}  BM25={h['bm25']}")
        print(f"        {h['text'][:80]}{'…' if len(h['text']) > 80 else ''}")

    if not use_llm:
        print("\n[--no-llm] 已跳过 LLM 调用。")
        return
    print("\n" + "─" * 12 + " 回答 " + "─" * 24)
    try:
        print(call_llm(build_messages(question, hits), cfg))
    except LLMConfigError as exc:
        print(f"[LLM 不可用] {exc}")
        print("提示：检索结果已在上方展示，可先用 --no-llm 验证检索链路。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="备料知识 · 裸问答原型（RAG）")
    parser.add_argument("--config", default=str(REPO_ROOT / "config/prototype.yaml"))
    parser.add_argument("--ask", help="单次提问后退出；缺省进入交互模式")
    parser.add_argument("--no-llm", action="store_true", help="只做检索，不调用 LLM")
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存，强制重建索引")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    chunks, vectors, _labels = build_or_load_index(cfg, force=args.rebuild)
    dense_index = build_dense_index(vectors)

    if args.ask:
        answer(args.ask, chunks, vectors, dense_index, cfg, use_llm=not args.no_llm)
        return 0

    print("备料知识问答（输入 q 退出）")
    while True:
        try:
            question = input("\n问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in {"q", "quit", "exit", "退出"}:
            break
        if not question:
            continue
        answer(question, chunks, vectors, dense_index, cfg, use_llm=not args.no_llm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
