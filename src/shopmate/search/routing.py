"""사진 검색 경로 선택의 단일 지점: 질의 모달리티 → 검색 경로, 실패 시 폴백, 결과 형식.

    사진만      → Qwen3-VL 이미지 질의 벡터
    사진 + 글   → Qwen3-VL fused 질의 벡터(사진 + Gemma가 만든 영어 검색문 retrieval_query_en)
    Qwen 실패   → 호출자가 기존 SigLIP+KURE RRF(fallback)로 폴백

글만 있는 검색은 이 모듈을 거치지 않는다(KURE, shop.search_semantic_result).
상품 쪽은 Qwen 문서 벡터 한 벌(config.QWEN3_VL_RECIPE, 기본 사진 + search_text)을 두 질의 경로가 공유한다.
필터는 호출자가 filter_resolution으로 판정한 하드 필터만 받는다. 사진 추정값(soft)은 이 경로에서 쓰지 않는다 —
사진 자체가 질의 벡터에 들어가기 때문이다.
"""

from __future__ import annotations

import sys
from typing import Any

from shopmate import config

# (표시 이름, 서비스 모듈, ranking 값). config.MULTIMODAL_BACKEND가 rrf면 통합 경로를 쓰지 않는다.
QWEN_BACKEND = ("Qwen3-VL", "qwen", "qwen3_vl_fused_image_text")


class BackendUnavailable(RuntimeError):
    """통합 임베딩 경로를 쓸 수 없다. 호출자는 기존 SigLIP+KURE RRF(fallback)로 폴백한다.

    str()은 모델에게 넘길 짧은 문장이다(토큰·내부 정보 최소화). 원인 전문은 detail에 있다.
    """

    def __init__(self, label: str, error: Exception):
        super().__init__(f"{label} 검색을 사용할 수 없습니다({type(error).__name__}).")
        self.label = label
        self.detail = f"{type(error).__name__}: {error}"


def multimodal_backend() -> tuple[str, str, str] | None:
    """설정된 통합 임베딩 백엔드. rrf면 None(바로 기존 경로를 쓴다)."""
    return QWEN_BACKEND if config.MULTIMODAL_BACKEND == "qwen" else None


def search_by_image(user_id: str, query_image_id: str, text: str | None = None, *,
                    filters: dict[str, Any] | None = None, limit: int = 10) -> dict[str, Any] | None:
    """사진(+글) 질의를 통합 임베딩 경로로 검색한다.

    text가 없으면 사진만으로 찾는다. 설정이 rrf면 None을 돌려준다.
    서비스 호출·검색이 실패하면 BackendUnavailable을 던진다.
    """
    backend = multimodal_backend()
    if backend is None:
        return None
    label, _, ranking = backend
    text = " ".join(str(text or "").split()) or None
    hard = {key: value for key, value in (filters or {}).items() if value is not None}
    try:
        from shopmate.search import qwen
        rows = qwen.find_similar(user_id, query_image_id, text, limit=limit, **hard)
    except Exception as error:  # noqa: BLE001 — 어떤 실패든 폴백 사유로 돌려준다
        unavailable = BackendUnavailable(label, error)
        print(f"[검색] {unavailable} SigLIP+KURE RRF로 폴백 — {unavailable.detail}", file=sys.stderr)
        raise unavailable from error
    products = [{
        "product_id": row["product_id"], "name": row.get("name"),
        "category": row.get("category"), "brand": row.get("brand"),
        "price": row.get("price"), "rating": row.get("rating"),
        "multimodal_score": row.get("score"),
        "object_bucket": row.get("object_bucket"), "thumbnail_key": row.get("thumbnail_key"),
    } for row in rows]
    return {
        "ranking": ranking,
        "route": "qwen_fused" if text else "qwen_image",
        "query_text": text,
        "hard_filters": hard,
        "products": products,
    }
