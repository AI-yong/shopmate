"""Use grounded Gemma item features in image retrieval without changing SQL filters."""

from __future__ import annotations

from typing import Any

REFERENCE_ATTRIBUTES = ("pattern", "length", "silhouette", "details")
MAX_FEATURE_LENGTH = 160
# The selected item's kind in English, appended to the query when the user didn't name one.
# The agent usually writes "similar to the ones in the image"; with the photo's group filter on,
# small items matched their exact category 0.36 of the time, 0.80 with this sentence appended
# (internal crop evaluation).
CATEGORY_EN = {
    "티셔츠": "t-shirt", "셔츠": "shirt", "니트": "knit sweater", "후드": "hoodie",
    "팬츠": "pants", "스커트": "skirt", "재킷": "jacket", "코트": "coat", "정장": "suit",
    "샌들": "sandals", "구두": "dress shoes", "부츠": "boots", "운동화": "sneakers",
    "모자": "cap", "가방": "bag", "스카프": "scarf", "벨트": "belt", "기타": "accessory",
}


def _short_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:MAX_FEATURE_LENGTH] or None


def clean_search_features(item: dict[str, Any], raw: Any) -> dict[str, dict[str, str]]:
    """Keep supported visible axes with both an English description and evidence.

    Confidence is not a calibrated accuracy score. Material, color, category,
    brands and OCR are deliberately outside this supplemental text contract.
    """
    if not isinstance(raw, dict):
        return {}
    features = {}
    for axis in REFERENCE_ATTRIBUTES:
        observed = item.get(axis)
        if not observed or observed == "해당없음":
            continue
        if axis == "details" and not isinstance(observed, list):
            continue
        entry = raw.get(axis)
        if not isinstance(entry, dict):
            continue
        text = _short_text(entry.get("text"))
        evidence = _short_text(entry.get("evidence"))
        if text and evidence:
            features[axis] = {"text": text, "evidence": evidence}
    return features


def target_noun(item: dict[str, Any] | None, query: str | None) -> str | None:
    """English kind of the selected item, or None when unknown or the query already names it.

    Any word of the kind counts ("knitwear" names a knit sweater, "shoes" names dress shoes).
    """
    noun = CATEGORY_EN.get((item or {}).get("category"))
    if not noun or not query:
        return None
    lowered = query.lower()
    if any(word in lowered for word in noun.split()):
        return None
    return noun


def build_search_query(
    query: str | None,
    item: dict[str, Any] | None,
    reference_attributes: list[str] | None = None,
    name_item: bool = False,
) -> dict[str, Any]:
    """Append only explicitly preserved reference axes to the user's query.

    None means no opt-in (older clients/cached analyses keep their old behavior).
    An empty list intentionally discards every reference attribute. A request
    without a query stays image-only. No model call or SQL filtering is added.
    name_item appends the selected item's kind; callers pass it only when the user
    did not name a kind (the same condition as the photo group filter).
    """
    base = " ".join((query or "").split()) or None
    supported = clean_search_features(item or {}, (item or {}).get("search_features_en"))
    selected = set(reference_attributes or [])
    used = {axis: supported[axis] for axis in REFERENCE_ATTRIBUTES
            if base and axis in selected and axis in supported}
    noun = target_noun(item, base) if name_item else None
    text = base
    if noun:
        text = f"{text.rstrip('. ')}. The target item is the {noun}."
    if used:
        descriptions = list(dict.fromkeys(feature["text"] for feature in used.values()))
        text = f"{text.rstrip('. ')}. Preserve these visible reference features: " + "; ".join(descriptions) + "."
    return {
        "query_text": text,
        "base_query_text": base,
        "target_item_named": noun,
        "reference_features_used": used,
        "reference_attributes_requested": [axis for axis in REFERENCE_ATTRIBUTES if axis in selected],
        "query_enriched": bool(used or noun),
    }


def item_for_fallback(
    item: dict[str, Any] | None, reference_attributes: list[str] | None,
) -> dict[str, Any] | None:
    """Keep the same selected axes when the unified backend falls back to RRF.

    A free-form caption may contain an excluded pattern/length. Remove it when
    an axis is omitted rather than reintroducing that attribute through text.
    """
    if not item or reference_attributes is None:
        return item
    selected = set(reference_attributes)
    result = dict(item)
    for axis in REFERENCE_ATTRIBUTES:
        if axis not in selected:
            result[axis] = [] if axis == "details" else None
    if any(axis not in selected for axis in REFERENCE_ATTRIBUTES):
        result["visual_description"] = None
    return result
