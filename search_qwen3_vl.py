"""Qwen3-VL Amazon Fashion document vector search."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

import config
import db_pg
from qwen3_vl_embedding import MODEL_NAME, MODEL_REVISION

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
    where, parameters = db_pg._product_filter_where(**filters)
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
