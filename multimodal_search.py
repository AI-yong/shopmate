"""KURE 텍스트 검색과 SigLIP 이미지 검색을 순위 기반으로 결합한다.

서로 다른 모델의 벡터나 원점수가 같은 척 더하지 않는다. 각 검색기의 순위만
RRF로 합쳐 modality gap과 점수 스케일 차이를 피한다. 세 갈래(사진·문장·사진 설명)는
같은 무게로 합친다.
"""

from __future__ import annotations

from typing import Any

import image_query_service
import multimodal_query

RRF_K = 60
POOL_SIZE = 50


def rrf_fuse(image_rows: list[dict[str, Any]],
             text_rows: list[dict[str, Any]], limit: int = 10,
             visual_text_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if not 1 <= limit <= 50:
        raise ValueError("limit은 1~50이어야 합니다.")
    merged: dict[str, dict[str, Any]] = {}

    def add(rows, branch, id_key):
        for rank, row in enumerate(rows, 1):
            product_id = str(row[id_key])
            item = merged.setdefault(product_id, {
                "product_id": product_id, "fusion_score": 0.0,
                "image_rank": None, "text_rank": None,
                "visual_text_rank": None,
            })
            item["fusion_score"] += 1 / (RRF_K + rank)
            item[f"{branch}_rank"] = rank
            if branch == "image":
                item.update({key: value for key, value in row.items()
                             if key not in {"score", "product_id"}})
            else:
                item.setdefault("name", row.get("name"))
                item.setdefault("category", row.get("category"))
                item.setdefault("gender", row.get("gender"))
                item.setdefault("brand", row.get("brand"))
                item.setdefault("price", row.get("price"))
                item.setdefault("rating", row.get("rating"))
                item.setdefault("review_count", row.get("review_count"))

    add(image_rows, "image", "product_id")
    add(text_rows, "text", "id")
    add(visual_text_rows or [], "visual_text", "id")
    ordered = sorted(merged.values(), key=lambda row: (
        -row["fusion_score"],
        row["image_rank"] if row["image_rank"] is not None else 10 ** 9,
        row["text_rank"] if row["text_rank"] is not None else 10 ** 9,
        row["visual_text_rank"] if row["visual_text_rank"] is not None else 10 ** 9,
        row["product_id"],
    ))[:limit]
    for row in ordered:
        row["score"] = row["fusion_score"]
        row["fusion_score"] = round(row["fusion_score"], 8)
    return ordered


# 기준 사진 속성으로 만든 필터를 결과가 모자랄 때 푸는 순서. 뒤에 있을수록 오래 남는다.
# 소재·색은 VLM 추정 정확도가 낮아 먼저 풀고, 종류는 마지막에 대분류로 넓힌 뒤 푼다.
REFERENCE_RELAX_ORDER = ("material", "color", "category")
DEFAULT_KEEP_FROM_REFERENCE = ("category",)


def reference_filters_from_item(visual_item: dict[str, Any] | None,
                                user_filters: dict[str, Any]) -> dict[str, Any]:
    """기준 사진에서 유지할 속성(DEFAULT_KEEP_FROM_REFERENCE)의 값을 꺼낸다. 사용자가 직접 말한 축은 건너뛴다."""
    result: dict[str, Any] = {}
    if not visual_item:
        return result
    for key in DEFAULT_KEEP_FROM_REFERENCE:
        if user_filters.get(key) is not None:
            continue
        value = visual_item.get(key)
        if value:
            result[key] = value
    return result


def _relax(reference: dict[str, Any], store) -> tuple[dict[str, Any], str | None]:
    """완화 한 단계. (남은 기준 필터, 푼 것의 이름) 을 돌려준다. 더 풀 게 없으면 (같은, None)."""
    for key in REFERENCE_RELAX_ORDER:
        if key not in reference:
            continue
        relaxed = dict(reference)
        if key == "category":
            group = store.group_of(reference["category"])
            relaxed.pop("category")
            if group and "group" not in relaxed:
                relaxed["group"] = group
                return relaxed, "category→group"
            relaxed.pop("group", None)
            return relaxed, "category"
        relaxed.pop(key)
        return relaxed, key
    if "group" in reference:
        relaxed = dict(reference)
        relaxed.pop("group")
        return relaxed, "group"
    return reference, None


def search(store, user_id: str, query_image_id: str, text: str | None = None, *,
           visual_item: dict[str, Any] | None = None,
           visual_source: str | None = None,
           soft_filters: dict[str, Any] | None = None,
           group: str | None = None, category: str | None = None,
           gender: str | None = None, brand: str | None = None,
           min_price: int | None = None, max_price: int | None = None,
           color: str | None = None, size: str | int | None = None,
           material: str | None = None,
           machine_washable: bool | None = None,
           in_stock: bool | None = None,
           exclude_category: str | None = None,
           exclude_color: str | None = None,
           exclude_material: str | None = None,
           limit: int = 10) -> dict[str, Any]:
    """사진 + 문장 검색.

    필터는 두 층이다.
    - 하드 필터: 사용자가 직접 말한 값(color=검은색, max_price…). 완화하지 않는다. 0건이면 0건.
    - 기준 필터(reference): 사진에서 VLM이 본 값 중 DEFAULT_KEEP_FROM_REFERENCE 축
      (category)과, 모델이 하드 필터에 넣었지만 사용자 문장에 없어 강등된 값(soft_filters).
      결과가 limit에 못 미치면 REFERENCE_RELAX_ORDER 순서로 하나씩 풀고 다시 검색한다.
    SigLIP·KURE 세 갈래는 같은 필터를 받는다.
    """
    plan = multimodal_query.build_query_plan(
        semantic_query=text, visual_item=visual_item, visual_source=visual_source)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("min_price는 max_price보다 클 수 없습니다.")
    pool = min(POOL_SIZE, max(limit * 5, limit))
    hard_filters = {
        key: value for key, value in {
            "gender": gender, "brand": brand, "min_price": min_price,
            "max_price": max_price, "color": color, "size": size,
            "material": material, "machine_washable": machine_washable,
            "in_stock": in_stock,
            "exclude_category": exclude_category,
            "exclude_color": exclude_color,
            "exclude_material": exclude_material,
        }.items() if value is not None
    }
    user_axes = {**hard_filters, "group": group, "category": category}
    reference = reference_filters_from_item(visual_item, user_axes)
    for key, value in (soft_filters or {}).items():
        if key in REFERENCE_RELAX_ORDER and user_axes.get(key) is None and value:
            reference[key] = value
    # 검은 니트 사진에 "검은색 말고"라고 하면 사진 기준 color=검은색은 사용자 제외 조건과
    # 정면으로 부딪친다. 0건이 된 뒤 푸는 대신 처음부터 기준으로 쓰지 않는다.
    for key in ("category", "color", "material"):
        if key in reference and reference[key] == hard_filters.get(f"exclude_{key}"):
            reference.pop(key)
    relaxed: list[str] = []

    def run(reference_now: dict[str, Any]):
        image_filters = dict(hard_filters)
        for key in ("color", "material"):
            if key in reference_now:
                image_filters[key] = reference_now[key]
        group_now = group or reference_now.get("group")
        category_now = category or reference_now.get("category")
        image_rows = image_query_service.find_similar(
            user_id, query_image_id, limit=pool, group=group_now,
            category=category_now, **image_filters)
        filters = dict(image_filters)
        if group_now:
            filters["group"] = group_now
        if category_now:
            filters["category"] = category_now
        semantic = None
        if plan["semantic_retrieval_query"]:
            semantic = store.search_semantic_result(
                plan["semantic_retrieval_query"], top_k=pool,
                min_score=None, min_results=0, **filters)
        text_rows = semantic["products"] if semantic and semantic["available"] else []
        visual_semantic = None
        if plan["visual_retrieval_query"]:
            visual_semantic = store.search_semantic_result(
                plan["visual_retrieval_query"], top_k=pool,
                min_score=None, min_results=0, **filters)
        visual_text_rows = (
            visual_semantic["products"]
            if visual_semantic and visual_semantic["available"] else [])
        rows = rrf_fuse(image_rows, text_rows, limit=limit,
                        visual_text_rows=visual_text_rows)
        available = bool(
            (semantic and semantic["available"])
            or (visual_semantic and visual_semantic["available"]))
        return rows, filters, available

    rows, filters, text_available = run(reference)
    while len(rows) < limit:
        reference_next, dropped = _relax(reference, store)
        if dropped is None:
            break
        reference = reference_next
        relaxed.append(dropped)
        rows, filters, text_available = run(reference)

    return {
        "results": rows,
        "ranking": (
            "rrf_siglip_kure" if text_available
            else "siglip_filtered"
            if not plan["semantic_retrieval_query"] and not plan["visual_retrieval_query"]
            else "siglip_only_fallback"),
        "semantic_query": plan["semantic_query"],
        "query_plan": plan,
        "hard_filters": filters,
        "user_filters": {key: value for key, value in user_axes.items() if value is not None},
        "reference_filters": reference,
        "reference_filters_relaxed": relaxed,
    }
