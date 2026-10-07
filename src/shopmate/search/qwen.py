"""Qwen3-VL 사진 검색: 상주 임베딩 서비스(services/qwen3_vl)로 질의 벡터를 만들고
PostgreSQL 상품 문서 벡터(product_multimodal_embeddings)에서 찾는다."""

from __future__ import annotations

import base64
import json
import os
from functools import lru_cache
from io import BytesIO
from typing import Any
from urllib import error, request

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from shopmate import config
from shopmate.search import photo_store
from shopmate.store import db

# 상품 벡터를 만든 모델. services/qwen3_vl/encoder.py 의 값과 같아야 한다.
MODEL_NAME = "Qwen/Qwen3-VL-Embedding-2B"
MODEL_REVISION = "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"
EMBEDDING_DIM = 1536

# 상품 벡터가 이보다 적으면(조리법을 바꾸고 아직 안 넣은 경우 등) 검색하지 않고 오류를 낸다.
# 빈 후보에서 찾으면 "결과 0개"가 조용히 나가 폴백도 일어나지 않는다.
MIN_COVERAGE = 0.95


@lru_cache(maxsize=4)
def coverage(dsn: str, recipe: str) -> tuple[int, int]:
    """(이 조리법 벡터가 있는 상품 수, 전체 상품 수). 프로세스당 한 번만 센다."""
    with psycopg.connect(dsn) as connection:
        covered, total = connection.execute(
            """SELECT
                 (SELECT count(DISTINCT p.product_id)
                    FROM product_multimodal_embeddings e
                    JOIN product_media_staging m ON m.media_id=e.media_id
                    JOIN products p ON p.source_item_id=m.source_item_id
                   WHERE e.model_name=%s AND e.model_revision=%s AND e.recipe=%s),
                 (SELECT count(*) FROM products)""",
            (MODEL_NAME, MODEL_REVISION, recipe)).fetchone()
    return int(covered), int(total)


def search(dsn: str, vector: Any, limit: int = 10, **filters) -> list[dict]:
    if not 1 <= limit <= 50:
        raise ValueError("limit은 1 이상 50 이하여야 합니다.")
    recipe = config.QWEN3_VL_RECIPE
    covered, total = coverage(dsn, recipe)
    if total and covered < MIN_COVERAGE * total:
        raise RuntimeError(
            f"Qwen3-VL {recipe} 상품 벡터가 {covered:,}/{total:,}개뿐입니다. "
            "이 조리법의 Qwen3-VL 상품 벡터를 product_multimodal_embeddings에 먼저 적재하세요.")
    where, parameters = db._product_filter_where(**filters)
    product_clause = where.removeprefix(" WHERE ") if where else "TRUE"
    with psycopg.connect(dsn) as connection:
        register_vector(connection)
        with connection.cursor() as cursor:
            cursor.execute(f"""WITH eligible AS MATERIALIZED (
              SELECT e.media_id,e.embedding,p.product_id
              FROM product_multimodal_embeddings e
              JOIN product_media_staging m ON m.media_id=e.media_id
              JOIN products p ON p.source_item_id=m.source_item_id
              WHERE e.model_name=%s AND e.model_revision=%s
                AND e.recipe=%s AND {product_clause}),
            nearest AS MATERIALIZED (
              SELECT media_id,product_id,1-(embedding <=> %s) AS score
              FROM eligible ORDER BY embedding <=> %s LIMIT %s)
            SELECT p.product_id,p.name,p.category,p.brand,p.price,p.rating,
                   m.object_bucket,m.thumbnail_key,n.score
            FROM nearest n
            JOIN product_media_staging m ON m.media_id=n.media_id
            JOIN products p ON p.product_id=n.product_id
            ORDER BY n.score DESC LIMIT %s""",
            (MODEL_NAME, MODEL_REVISION, recipe, *parameters,
             vector, vector, min(limit * 4, 200), limit))
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]


# --- 상주 임베딩 서비스 호출 ---

SERVICE_URL = os.environ.get("QWEN3_VL_SERVICE_URL", "http://127.0.0.1:8092").rstrip("/")
SERVICE_TIMEOUT_SECONDS = float(os.environ.get("QWEN3_VL_SERVICE_TIMEOUT_SECONDS", "30"))


def _call(path: str, payload: dict) -> np.ndarray:
    body = json.dumps(payload).encode("utf-8")
    call = request.Request(
        f"{SERVICE_URL}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(call, timeout=SERVICE_TIMEOUT_SECONDS) as response:
            result = json.load(response)
    except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Qwen3-VL 임베딩 서비스 호출 실패({SERVICE_URL}{path}): {exc}. "
            "`.venv-qwen/bin/python services/qwen3_vl/server.py --device auto`로 "
            "상주 서비스를 먼저 실행하세요.") from exc
    if result.get("error"):
        raise RuntimeError(f"Qwen3-VL 임베딩 서비스 오류: {result['error']}")
    vector = np.asarray(result.get("embedding"), dtype=np.float32)
    if vector.shape != (EMBEDDING_DIM,) or not np.isfinite(vector).all():
        raise RuntimeError("Qwen3-VL 임베딩 서비스가 잘못된 벡터를 반환했습니다.")
    return vector


def _image_payload(image) -> str:
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=95)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def image_vector(image) -> np.ndarray:
    return _call("/embed/image", {"image_base64": _image_payload(image)})


def fused_vector(image, text: str) -> np.ndarray:
    return _call("/embed/fused", {
        "image_base64": _image_payload(image),
        "text": " ".join(str(text or "").split()),
    })


def find_similar(user_id: str, query_image_id: str, text: str | None = None,
                 limit: int = 10, **filters):
    image = photo_store.load_query_image(user_id, query_image_id)
    normalized = " ".join(str(text or "").split())
    vector = fused_vector(image, normalized) if normalized else image_vector(image)
    return search(config.APP_SHOP_DSN, vector, limit, **filters)
