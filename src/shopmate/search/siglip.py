"""SigLIP 2 이미지 검색. Qwen3-VL 사진 검색이 실패했을 때 쓰는 폴백 경로다.

업로드 사진을 SigLIP 2 이미지 벡터로 바꿔, SQL 조건으로 좁힌 상품 사진 벡터와 비교한다.
KURE 상품 설명 검색과는 순위로만 합친다(search/fallback.py).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import psycopg
import torch
from pgvector.psycopg import register_vector
from PIL import Image
from transformers import AutoModel, AutoProcessor

from shopmate import config
from shopmate.store import db

def choose_device(requested: str) -> str:
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


DEFAULT_MODEL = config.SIGLIP_MODEL_NAME
DEFAULT_REVISION = config.SIGLIP_MODEL_REVISION


def tensor_features(value: Any) -> torch.Tensor:
    if isinstance(value, torch.Tensor):
        return value
    if hasattr(value, "pooler_output"):
        return value.pooler_output
    raise TypeError(f"지원하지 않는 get_image_features 반환형: {type(value)!r}")


# category_groups.group_name 값. 도구 enum·store.group_of 가 넘기는 대분류가 이 이름이다.
GROUP_NAMES = ("상의", "하의", "아우터", "신발", "가방·액세서리")


def normalize(value: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(value.float(), dim=-1)


class SiglipQueryEncoder:
    """프로세스당 모델을 한 번만 올려 반복 질의에 재사용한다."""

    def __init__(self, model_name: str = DEFAULT_MODEL,
                 revision: str = DEFAULT_REVISION, device: str = "auto"):
        self.model_name = model_name
        self.revision = revision
        self.device = choose_device(device)
        self.processor = AutoProcessor.from_pretrained(
            model_name, revision=revision or None)
        self.model = AutoModel.from_pretrained(
            model_name, revision=revision or None).eval().to(self.device)

    def encode_image(self, image: Image.Image) -> Any:
        """검증·정규화가 끝난 메모리 이미지를 경로 왕복 없이 인코딩한다."""
        with torch.inference_mode():
            inputs = self.processor(images=[image.convert("RGB")], return_tensors="pt")
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            vector = normalize(tensor_features(self.model.get_image_features(**inputs)))
        return vector.cpu().numpy()[0]


@lru_cache(maxsize=2)
def get_query_encoder(model_name: str = DEFAULT_MODEL,
                      revision: str = DEFAULT_REVISION,
                      device: str = "auto") -> SiglipQueryEncoder:
    """서버 요청마다 수백 MB 모델을 다시 읽지 않도록 캐시한다."""
    return SiglipQueryEncoder(model_name, revision, device)


def search(dsn: str, vector: Any, limit: int,
           group: str | None = None,
           category: str | None = None, *, gender: str | None = None,
           brand: str | None = None, min_price: int | None = None,
           max_price: int | None = None, color: str | None = None,
           size: str | int | None = None, material: str | None = None,
           machine_washable: bool | None = None,
           in_stock: bool | None = None,
           exclude_category: str | None = None,
           exclude_color: str | None = None,
           exclude_material: str | None = None) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("limit은 1 이상 50 이하여야 합니다.")
    conditions = ["e.model_name=%s", "e.model_revision=%s",
                  "p.source_name='amazon_fashion_2023'"]
    parameters: list[Any] = [DEFAULT_MODEL, DEFAULT_REVISION]
    if group is not None and group not in GROUP_NAMES:
        raise ValueError(f"지원하지 않는 대분류입니다: {group}")
    # 텍스트·이미지 검색이 서로 다른 조건을 쓰지 않도록 일반 상품 검색과 같은
    # SQL 필터 계약을 재사용한다. 선필터 뒤에만 벡터 거리를 계산한다.
    product_where, product_parameters = db._product_filter_where(
        group=group, category=category, gender=gender, brand=brand,
        min_price=min_price, max_price=max_price, color=color, size=size,
        material=material, machine_washable=machine_washable,
        in_stock=in_stock,
        exclude_category=exclude_category, exclude_color=exclude_color,
        exclude_material=exclude_material)
    if product_where:
        conditions.append(product_where.removeprefix(" WHERE "))
        parameters.extend(product_parameters)
    where = " AND ".join(conditions)
    # 15,000개에서는 측정 결과 정확 검색이 충분히 빨랐다. MATERIALIZED는 이 선택을
    # 명시하며 HNSW를 사용하지 않는다. 60만 부하 시험에서 ANN 우위가 확인되기 전에는
    # 인덱스 경로로 바꾸지 않는다.
    # 이미지가 여러 장으로 늘어나도 상품 하나가 결과를 독점하지 않도록 우선 넉넉히
    # 후보를 뽑은 뒤 상품별 가장 가까운 미디어 한 장만 남긴다.
    candidate_limit = min(limit * 4, 200)
    with psycopg.connect(dsn) as connection:
        register_vector(connection)
        with connection.cursor() as cursor:
            cursor.execute(f"""WITH eligible AS MATERIALIZED (
              SELECT e.media_id,e.embedding,m.source_item_id,p.product_id
              FROM product_media_embeddings e
              JOIN product_media_staging m ON m.media_id=e.media_id
              JOIN products p ON p.source_item_id=m.source_item_id
              JOIN category_groups cg ON cg.category=p.category
              WHERE {where}),
            nearest AS MATERIALIZED (
              SELECT media_id,source_item_id,product_id,1-(embedding <=> %s) AS score
              FROM eligible ORDER BY embedding <=> %s LIMIT %s),
            one_per_product AS (
              SELECT *,row_number() OVER
                (PARTITION BY source_item_id ORDER BY score DESC,media_id) AS media_rank
              FROM nearest)
              SELECT p.product_id,p.name,p.category,p.gender,p.brand,
                     p.price,p.rating,p.review_count,n.score
              FROM one_per_product n JOIN products p ON p.product_id=n.product_id
              WHERE n.media_rank=1 ORDER BY n.score DESC LIMIT %s""",
              (*parameters, vector, vector, candidate_limit, limit))
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
