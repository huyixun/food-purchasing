"""★③ 向量化器与索引（自 examples/rag_prototype.py 抽取）。

双通道嵌入：
- 配置了 embedding_model → 走供应商 /embeddings 接口
- 否则 → 本地哈希词袋兜底（确定性、离线可跑），仅供验证链路，
  上线前必须换成真实嵌入（本地 bge 类或 API），本模块接口不用变

FAISS 用法：IndexFlatIP + L2 归一化向量 = 余弦相似度。
faiss 缺失时退化为 numpy 暴力检索，接口一致（私有化环境兜底）。
阶段 3 计划迁移 pgvector，届时本模块只保留 embed_texts。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import jieba
import numpy as np

from prep_assistant.agents.llm.gateway import _api_key

try:
    import faiss

    HAS_FAISS = True
except ImportError:  # pragma: no cover
    HAS_FAISS = False

LOCAL_EMBED_DIM = 512  # 本地兜底向量的维度
REPO_ROOT = Path(__file__).resolve().parents[4]  # rag → agents → prep_assistant → src → 仓库根


def _local_embed(texts: list[str]) -> np.ndarray:
    """本地占位嵌入：jieba 分词后按 token 哈希落桶成词袋向量，L2 归一化。"""
    mat = np.zeros((len(texts), LOCAL_EMBED_DIM), dtype="float32")
    for i, text in enumerate(texts):
        for token in jieba.lcut(text):
            slot = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % LOCAL_EMBED_DIM
            mat[i, slot] += 1.0
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def embed_texts(texts: list[str], cfg: dict) -> np.ndarray:
    """把一批文本向量化，返回 float32 矩阵（每行已 L2 归一化）。"""
    model = cfg["llm"].get("embedding_model") or ""
    if not model:
        return _local_embed(texts)
    url = cfg["llm"]["base_url"].rstrip("/") + "/embeddings"
    resp = httpx.post(
        url,
        json={"model": model, "input": texts},
        headers={"Authorization": f"Bearer {_api_key(cfg)}"},
        timeout=cfg["llm"].get("timeout_seconds", 60),
    )
    resp.raise_for_status()
    vecs = np.array([item["embedding"] for item in resp.json()["data"]], dtype="float32")
    return _normalize(vecs)


def build_or_load_index(
    cfg: dict,
    force: bool = False,
    extra_sources: list[tuple[str, Path]] | None = None,
) -> tuple[list[str], np.ndarray, list[str]]:
    """构建（或从缓存加载）切片 + 向量矩阵 + 每片的来源标签。

    索引内容 = 内置样例（cfg.source）+ 后台上传的全部知识来源。
    extra_sources 形如 [(展示名, md 文件路径), ...]，由路由层从
    knowledge_sources 表读出传入；上传/删除后路由会 force 重建。

    缓存键 = 全部源文件内容哈希 + 切片参数 + 嵌入通道 + 切片器版本。
    知识没变就不重复花嵌入的钱。返回 (chunks, vectors, labels)，
    三者一一对应；vectors 已归一化，labels 为每片的来源展示名。
    """
    from prep_assistant.agents.rag.chunker import CHUNKER_VERSION, chunk_markdown

    cache_dir = REPO_ROOT / cfg["cache_dir"]
    size = cfg["retrieval"]["chunk_size"]
    embed_channel = "api" if cfg["llm"].get("embedding_model") else "local"

    # 汇总全部来源：内置样例在前，上传来源按 id 序在后
    sources: list[tuple[str, Path]] = [("内置样例", REPO_ROOT / cfg["source"])]
    sources.extend(extra_sources or [])
    texts: list[str] = []
    for _, path in sources:
        texts.append(path.read_text(encoding="utf-8"))

    combined_hash = hashlib.sha256(
        "\x00".join(texts).encode("utf-8")
    ).hexdigest()[:16]
    fingerprint = f"{combined_hash}-{size}-{embed_channel}-{CHUNKER_VERSION}"

    cache_file = cache_dir / f"{fingerprint}.npz"
    if cache_file.exists() and not force:
        blob = np.load(cache_file, allow_pickle=False)
        chunks = json.loads(blob["meta_json"].item())
        labels = json.loads(blob["labels_json"].item())
        return chunks, blob["vectors"], labels

    chunks: list[str] = []
    labels: list[str] = []
    for (label, _), text in zip(sources, texts):
        for chunk in chunk_markdown(text, size):
            chunks.append(chunk)
            labels.append(label)
    vectors = embed_texts(chunks, cfg) if chunks else np.zeros((0, LOCAL_EMBED_DIM), dtype="float32")
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_file,
        vectors=vectors,
        meta_json=np.array(json.dumps(chunks, ensure_ascii=False)),
        labels_json=np.array(json.dumps(labels, ensure_ascii=False)),
    )
    return chunks, vectors, labels


def build_dense_index(vectors: np.ndarray):
    """建 FAISS 向量索引；无 faiss 时返回 None（检索函数走 numpy 路径）。"""
    if HAS_FAISS:
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        return index
    return None
