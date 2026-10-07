"""임베딩 읽기 — 상품 문서 벡터(products.embedding)와 질의 모델.

`store/shop.py` 가 쓰는 두 함수.

    load_matrix(conn)    -> (ids, matrix)   문서 벡터
    query_backend(conn)  -> backend         질의를 같은 공간으로 인코딩

## 모델과 벡터는 한 짝이다

문서를 A 모델로 굽고 질의를 B 모델로 만들면 **조용히** 엉뚱한 결과가 나온다.
그래서 상품 벡터를 만들 때 `meta` 에 모델 이름과 차원을 적고, 여기서는
그 쪽지를 읽어 같은 모델로 질의 백엔드를 만든다. `Store._ensure_vectors()` 가
두 차원을 대조해 어긋나면 멈춘다.

예전에 이 가드가 없어서, 실험 스크립트가 운영 DB 의 문서 벡터만 갈아끼우고
쪽지를 안 고쳐 768차원 문서에 384차원 질의가 남아 있었다.
"""

from __future__ import annotations

import numpy as np

_MODEL_CACHE = {}


def vector_array(value):
    """psycopg의 pgvector.Vector와 일반 list/ndarray를 모두 float32로 바꾼다."""
    if hasattr(value, "to_numpy"):
        value = value.to_numpy()
    return np.asarray(value, dtype=np.float32)


def read_meta(conn):
    return {row[0]: row[1] for row in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'embed%%'")}


def load_matrix(conn):
    """(ids, matrix). 벡터가 하나도 없으면 (None, None).

    L2 정규화된 float32 로 돌려준다 — `Store.semantic_scores` 가 내적을
    코사인으로 쓰기 때문이다. 벡터를 만들 때 정규화해 넣지만, 다른 경로로
    들어온 벡터가 섞일 수 있으므로 여기서 한 번 더 맞춘다.
    """
    rows = conn.execute(
        "SELECT product_id, embedding FROM products"
        " WHERE embedding IS NOT NULL ORDER BY product_id").fetchall()
    if not rows:
        return None, None

    ids = [r[0] for r in rows]
    matrix = np.asarray([vector_array(r[1]) for r in rows],
                        dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, matrix / norms


class SentenceTransformerBackend:
    """질의를 문서와 같은 공간으로 인코딩한다."""

    def __init__(self, model_name):
        self.model_name = model_name
        model = _MODEL_CACHE.get(model_name)
        if model is None:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(model_name)
            _MODEL_CACHE[model_name] = model
        self.model = model
        get_dim = getattr(model, "get_embedding_dimension",
                          model.get_sentence_embedding_dimension)
        self.dim = get_dim()

    def encode(self, texts):
        vectors = self.model.encode(list(texts), convert_to_numpy=True,
                                    normalize_embeddings=True,
                                    show_progress_bar=False)
        return vectors.astype(np.float32)


def query_backend(conn):
    """DB 가 기억하는 모델로 질의 백엔드를 만든다.

    설정(config.EMBED_MODEL_NAME)이 아니라 **DB 의 meta** 를 따른다.
    설정을 먼저 바꾸고 아직 안 구운 상태에서 설정을 따르면, 문서와 질의가
    다른 모델이 되는데 아무도 모른다.
    """
    meta = read_meta(conn)
    if meta.get("embed_status") in {"building", "incomplete"}:
        raise RuntimeError(
            f"임베딩 상태가 {meta['embed_status']}입니다. "
            "상품 벡터(products.embedding)를 EMBED_MODEL_NAME 모델로 끝까지 "
            "다시 만든 뒤 의미 검색을 켜세요.")
    model_name = meta.get("embed_model")
    if not model_name:
        raise RuntimeError(
            "meta 에 embed_model 이 없습니다 — 아직 상품 벡터를 만들지 않았습니다.\n"
            "  상품 벡터(products.embedding)를 EMBED_MODEL_NAME 모델로 다시 만들어야 합니다.")
    return SentenceTransformerBackend(model_name)
