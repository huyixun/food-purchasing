"""★④ 混合检索器（自 examples/rag_prototype.py 抽取）。

稀疏（BM25）管专有名词精确命中，稠密（向量）管换种说法也能召回；
两路分数各自归一化后加权融合，权重是管理员可调的第一个旋钮。

性能注记：BM25 语料每次调用都重建，知识库只有几十段时无感；
阶段 3 知识库上量后应缓存 BM25 实例（随索引缓存一起失效）。
"""

from __future__ import annotations

import jieba
import numpy as np
from rank_bm25 import BM25Okapi


def hybrid_search(
    question: str,
    chunks: list[str],
    vectors: np.ndarray,
    dense_index,
    cfg: dict,
    labels: list[str] | None = None,
) -> list[dict]:
    """混合检索，返回 top_k 条 {text, source, dense, bm25, score}，按融合分降序。

    labels 与 chunks 一一对应（来源展示名）；不传时来源记为配置里的内置样例。
    """
    r = cfg["retrieval"]
    top_k = min(r["top_k"], len(chunks))
    default_source = cfg.get("source", "未知")

    # 稀疏通道：BM25（中文需先分词；语料与问题必须用同一套分词器）
    corpus = [jieba.lcut(c) for c in chunks]
    bm25 = BM25Okapi(corpus)
    bm25_scores = bm25.get_scores(jieba.lcut(question))
    bmax = bm25_scores.max()
    bm25_norm = bm25_scores / bmax if bmax > 0 else bm25_scores

    # 稠密通道：问题向量化后查索引（或 numpy 兜底做内积 = 余弦）
    from prep_assistant.agents.rag.indexer import embed_texts

    q_vec = embed_texts([question], cfg)
    if dense_index is not None:
        dense_scores = dense_index.search(q_vec, len(chunks))[0][0]
    else:
        dense_scores = vectors @ q_vec[0]

    fused = r["dense_weight"] * dense_scores + r["bm25_weight"] * bm25_norm
    order = np.argsort(fused)[::-1][:top_k]
    return [
        {
            "text": chunks[i],
            "source": labels[i] if labels and i < len(labels) else default_source,
            "dense": round(float(dense_scores[i]), 4),
            "bm25": round(float(bm25_norm[i]), 4),
            "score": round(float(fused[i]), 4),
        }
        for i in order
    ]
