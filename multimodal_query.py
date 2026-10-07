"""멀티모달 사용자 요청을 독립된 검색 신호로 조립한다.

`비슷한`은 임베딩할 의미가 아니라 기준 이미지를 사용하는 검색 연산이다.
사용자가 말한 의미 선호와 VLM이 본 사진 설명을 섞지 않고 각각 보존해야
트레이스에서 어느 신호가 검색에 쓰였는지 구분할 수 있다.
"""

from __future__ import annotations

import re
from typing import Any

# "비슷한"은 사진 유사 검색을 켜는 연산자다. 모델이 이 말을 semantic_query에 흘려 넣으면
# 내용 없는 문장("유사한 재질")이 임베딩되어 KURE 갈래가 잡음이 된다. 폐쇄 집합 4개만 걷어낸다.
_SIMILARITY_OPERATORS = re.compile(
    r"(이|그|저|해당|첨부|사진|이미지)?\s*(사진|이미지|것|거)?\s*(과|와|이랑|랑|처럼)?\s*"
    r"(비슷한|유사한|닮은|같은)\s*")


def strip_similarity_operators(text: str | None) -> tuple[str | None, bool]:
    """semantic_query에서 유사 연산자 표현을 제거한다. (남은 문장, 제거 여부)."""
    if not isinstance(text, str) or not text.strip():
        return None, False
    stripped = _SIMILARITY_OPERATORS.sub(" ", text)
    stripped = " ".join(stripped.split()).strip(" ,.") or None
    return stripped, stripped != " ".join(text.split())


def _text(value: Any, max_length: int = 300) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:max_length]


def visual_query_from_item(item: dict[str, Any] | None) -> str | None:
    """VLM item을 KURE용 사진 설명으로 만든다. 하드 필터로 사용하지 않는다."""
    if not item:
        return None
    parts: list[str] = []
    caption = _text(item.get("visual_description"))
    if caption:
        parts.append(caption)
    for key in ("category", "color", "material", "pattern", "length", "silhouette"):
        value = _text(item.get(key), 60)
        if value and value not in parts:
            parts.append(value)
    for key in ("details",):
        values = item.get(key) or []
        if isinstance(values, list):
            parts.extend(value for raw in values if (value := _text(raw, 60)))
    # 순서를 유지하며 중복을 제거한다.
    unique = list(dict.fromkeys(parts))
    return " ".join(unique)[:500] or None


def build_query_plan(*, semantic_query: str | None = None,
                     visual_item: dict[str, Any] | None = None,
                     visual_source: str | None = None) -> dict[str, Any]:
    """검색 실행과 트레이스 로그가 함께 쓰는 명시적 query plan을 반환한다.

    visual_source: "vlm_inferred"(사진 설명 사용) / "unavailable"(VLM 실패로 못 씀) /
    None(사진 설명 없이 요청). 로그에서 "안 쓴 것"과 "못 쓴 것"을 구분한다.
    """
    semantic = _text(semantic_query, 200)
    if visual_source is None and visual_item:
        visual_source = "vlm_inferred"
    return {
        "semantic_query": semantic,
        # 기준 대비 상대 변경("원본보다 밝은")을 비교하는 속성 비교기가 없다.
        # 그런 문장을 임베딩하면 적용된 척만 하게 되므로 항상 False로 알린다.
        "relative_applied": False,
        "semantic_retrieval_query": semantic,
        "visual_retrieval_query": visual_query_from_item(visual_item),
        "visual_source": visual_source,
    }
