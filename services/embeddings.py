"""可插拔 embedding —— 给 event_store 向量精排提供文本向量

默认 deterministic 1024-dim hash embedding：零成本、纯 Python、跨进程结果一致。
够用是因为 event_store 的硬过滤（实体 ∩ 时间 ∩ severity）已经做了重活，向量只是 marginal 精排。

升级路径（任选一）：
- env INVEST_EMBEDDING_PROVIDER=openai + OPENAI_API_KEY → 用 text-embedding-3-small
  （1536 维默认；本模块统一截到 1024 维，跟 event_store 默认对齐）
- 后续接 sentence-transformers / voyage / cohere 时在本模块加 if 分支即可
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
from functools import lru_cache
from typing import List, Optional

log = logging.getLogger(__name__)

DEFAULT_DIM = 1024


def embed_text(text: str, *, dim: int = DEFAULT_DIM) -> List[float]:
    """根据 env 选 provider。失败一律退回 hash embedding。"""
    provider = os.getenv("INVEST_EMBEDDING_PROVIDER", "hash").lower()
    text = (text or "").strip()
    if not text:
        return [0.0] * dim

    if provider == "openai":
        v = _openai_embed(text, dim=dim)
        if v is not None:
            return v
        log.warning("openai embedding 失败，退回 hash provider")
    return _hash_embed(text, dim=dim)


def _hash_embed(text: str, *, dim: int) -> List[float]:
    """确定性 hash embedding —— 用多个 sha256 拼字节，转 float32，L2 归一化

    特性：
    - 同样输入永远同样输出（跨进程、跨次启动）
    - 完全相同文本距离为 0
    - 不同文本距离接近 sqrt(2) ≈ 1.414（cosine 距离约 1.0）
    - 几乎没有语义近似能力 —— 但事件归一化 claim 文本已经很短很标准化，
      硬过滤 + 这一层足够先用，等真上 OpenAI key 时无缝切换
    """
    # 拼足够字节
    needed_bytes = dim * 4
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    buf = b""
    counter = 0
    while len(buf) < needed_bytes:
        h = hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        buf += h
        counter += 1
    buf = buf[:needed_bytes]
    floats = list(struct.unpack(f"{dim}I", buf))
    # 把 uint32 映射到 (-1, 1)
    vec = [(f / 0xFFFFFFFF) * 2.0 - 1.0 for f in floats]
    # L2 归一化（保证 cosine 距离意义清晰）
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _openai_embed(text: str, *, dim: int) -> Optional[List[float]]:
    """OpenAI text-embedding-3-small 调用。失败返回 None"""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI
        client = _get_openai_client(api_key)
        resp = client.embeddings.create(
            model=os.getenv("INVEST_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            input=text,
            dimensions=dim,
        )
        return list(resp.data[0].embedding)
    except Exception as e:
        log.warning(f"openai embed 调用失败: {type(e).__name__}: {e}")
        return None


@lru_cache(maxsize=1)
def _get_openai_client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key)
