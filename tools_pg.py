"""LLM 이 호출할 Tool 계층.

TOOLS(모델이 읽는 스키마), validate_call(실행 직전 인자 검증), Toolbox(실행기)로 이루어진다.
Toolbox 메서드는 얇게 둔다. 취소·반품 가능 여부 같은 정책 판단은 store_pg.Store 한 곳에서 하고,
여기서는 그 결과를 ok()/fail() 모양으로 옮긴다. 판단이 두 곳에 있으면 can_cancel 과
cancel_order 의 판단이 갈라진다.

스키마의 description 은 모델이 어떤 Tool 을 고를지 판단하는 근거이므로 애매하게 쓰지 않는다.
"""

import copy
import inspect
import re
import sys
import traceback
import uuid

import config
import db_pg as db
import retrieval
from store_pg import Store

# Tool enum의 단일 출처는 PostgreSQL 카탈로그(shop)입니다. 서버 시작 시 현재 DB 값을 읽어
# 모델과 검색기가 같은 값 집합을 사용하게 합니다.
_CATALOG_META = db.read_catalog_metadata()
_ENUMS = _CATALOG_META["enums"]
MATERIAL_NAMES = _ENUMS["material"]
GROUP_NAMES = list(_CATALOG_META["category_groups"])
CATEGORY_NAMES = [
    category
    for categories in _CATALOG_META["category_groups"].values()
    for category in categories
]
GROUP_OF_CATEGORY = {category: group
                     for group, categories in _CATALOG_META["category_groups"].items()
                     for category in categories}


def photo_item_group(visual_item, filters):
    """사진에서 고른 아이템의 대분류. 사용자가 품목·대분류를 말했으면 None.

    Qwen 사진 검색은 VLM 소분류를 어디에도 쓰지 않아, 운동화를 골라도 셔츠가 나왔다
    (일반 검색문 + 원본 사진이면 작은 아이템의 같은 대분류 비율 0.00). 대분류는 VLM이 틀릴
    일이 드물어 SQL 필터로 건다. 가산점(top-50 재정렬)은 후보에 같은 대분류가 아예 없어
    효과가 없었다(크롭 비교 측정).
    """
    if not visual_item or filters.get("group") or filters.get("category"):
        return None
    return GROUP_OF_CATEGORY.get(visual_item.get("category"))
# 사용자가 흔히 쓰는 말 -> category. search_product 의 category 설명과 같은 매핑이다.
CATEGORY_ALIASES = {"바지": "팬츠", "청바지": "팬츠", "슬랙스": "팬츠", "반바지": "팬츠",
                    "치마": "스커트", "자켓": "재킷", "스니커즈": "운동화", "후디": "후드",
                    "블라우스": "셔츠"}


def categories_in_text(text):
    """사용자 문장에 직접 나온 카탈로그 품목(category). "티셔츠" 안의 "셔츠"처럼 긴 낱말에 포함된
    짧은 낱말은 세지 않는다. '기타'는 품목 이름이 아니라서 제외한다."""
    text = text or ""
    words = sorted({*[c for c in CATEGORY_NAMES if c != "기타"], *CATEGORY_ALIASES},
                   key=len, reverse=True)
    found = []
    for word in words:
        start = text.find(word)
        if start < 0:
            continue
        text = text[:start] + " " * len(word) + text[start + len(word):]
        category = CATEGORY_ALIASES.get(word, word)
        if category not in found:
            found.append(category)
    return found


def fill_category_from_user(name, arguments, texts):
    """모델이 search_product 에서 category 를 비웠는데 사용자가 품목을 직접 말했으면 채운다.
    group 만 넣었고 사용자가 말한 품목이 그 대분류 안이면 category 로 좁힌다("재킷" -> group=아우터).

    "15만원 이하 남성 검은색 운동화 … 270 사이즈로 담아줘"에서 모델이 category 를 빼는 일이 반복됐다
    (size 가 신발을 뜻한다고 보고 생략). 필수 조건 확인이 인자만 보면 "운동화"라고 말한 사용자에게
    종류를 되묻는다. texts 는 최근 발화가 먼저다(filter_resolution.user_turns). 품목이 나온 가장
    최근 문장 하나만 보고, 그 문장에 품목이 정확히 하나일 때만 채운다.
    """
    if name != "search_product" or arguments.get("category") or arguments.get("product_name"):
        return arguments
    group = arguments.get("group")
    for text in [t for t in (texts or []) if isinstance(t, str)]:
        found = categories_in_text(text)
        if len(found) == 1:
            if not group:
                return {**arguments, "category": found[0]}
            # "재킷 보여줘"를 group=아우터로 넓혀 부른 경우: 그 대분류 안의 품목이면 category 로 좁힌다.
            if found[0] in (_CATALOG_META["category_groups"].get(group) or []):
                narrowed = {k: v for k, v in arguments.items() if k != "group"}
                return {**narrowed, "category": found[0]}
            return arguments
        if found:
            break
    return arguments


GENDER_NAMES = _ENUMS["gender"]
COLOR_NAMES = _ENUMS["color"]


# ======================================================================
# 반환 형식
#
# 모든 Tool 은 같은 모양으로 돌려줍니다. 형식이 제각각이면 모델이 결과를
# 해석하는 데 실패하고, 프롬프트로 일일이 설명해 줘야 합니다.
#
#     {"success": bool, "data": ..., "message": str}
#
# message 는 모델이 사용자에게 그대로 옮겨도 되는 한국어 문장으로 쓰세요.
# 실패했을 때 "왜" 실패했는지가 들어 있어야 에이전트가 대안을 안내합니다.
# ======================================================================

# 응답 크기 상한. 모델이 limit=999 나 20개 비교를 요청하면 토큰이 터진다.
MAX_SEARCH_RESULTS = 50
MAX_COMPARE_ITEMS = 6
MAX_ORDER_RESULTS = 20


def size_schema(description):
    """숫자형 옛 사이즈와 FREE/S/M/L 문자열을 함께 받는 Tool 계약."""
    return {
        "anyOf": [
            {"type": "integer", "minimum": 0},
            {"type": "string", "minLength": 1, "maxLength": 20},
        ],
        "description": description,
    }


# 긍정 필터와 짝을 이루는 제외 필터 축. filter_resolution.REFERENCE_AXES 와 같다.
EXCLUDABLE_AXES = ("category", "color", "material")


def exclude_schema():
    """'검은색 말고' 같은 부정 조건을 긍정 필터와 따로 표현하는 인자.

    부정 조건을 넣을 자리가 없으면 모델이 color=검은색을 넣어 정반대 결과가 나온다.
    """
    def one(label, names):
        return {
            "type": "string", "enum": names,
            "description": (
                f"사용자가 '~ 말고', '~ 빼고', '~ 싫어'처럼 원하지 않는다고 한 {label}. "
                f"그 {label} 옵션이 있는 상품을 결과에서 뺀다. 이 값을 같은 축의 긍정 인자에 넣지 않는다"),
        }
    return {
        "exclude_category": one("소분류", CATEGORY_NAMES),
        "exclude_color": one("색상", COLOR_NAMES),
        "exclude_material": one("소재", MATERIAL_NAMES),
    }


# 사이즈 표기 통일. DB 는 사이즈를 문자열("270", "M", "2XL", "ONE_SIZE", "FREE")로 둔다.
# 검증에서 "270" 이 정수 270 으로 바뀌면 사이즈 목록(문자열)과 비교가 어긋나 "270 은 없는 사이즈,
# 가능한 사이즈 …270…" 같은 모순 안내가 나갔고, "m"·"XXL" 은 검색에서만 0건이 됐다.
_SIZE_ALIASES = {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "ONESIZE": "ONE_SIZE",
                 "ONE SIZE": "ONE_SIZE", "ONE-SIZE": "ONE_SIZE", "FREE SIZE": "FREE",
                 "FREESIZE": "FREE", "F": "FREE", "프리": "FREE", "프리사이즈": "FREE"}


def normalize_size(value):
    """사이즈 하나를 DB 표기 문자열로. 잘못된 값이면 ValueError."""
    text = " ".join(str(value).split()).upper()
    if not text:
        raise ValueError("사이즈가 비어 있습니다.")
    if text.startswith("-"):
        raise ValueError(f"사이즈는 음수일 수 없습니다. 받은 값: {value!r}")
    text = _SIZE_ALIASES.get(text, text)
    if text.isdigit():
        text = str(int(text))          # "0270" -> "270"
    return text


def _normalize_sizes(arguments):
    """맨 위 size 와 items[].size 를 모두 통일한다. 오류 문장 또는 None."""
    try:
        if arguments.get("size") is not None:
            arguments["size"] = normalize_size(arguments["size"])
        for row in arguments.get("items") or []:
            if isinstance(row, dict) and row.get("size") is not None:
                row["size"] = normalize_size(row["size"])
    except ValueError as error:
        return str(error)
    return None


UNKNOWN_BRAND = "브랜드 미상"


def _brand(value):
    """모델에게 넘기는 브랜드. 승격 때 채운 자리표시값("브랜드 미상")은 null 로 보낸다.
    그대로 두면 모델이 "브랜드 미상 제품" 을 브랜드처럼 소개한다."""
    return None if value in (None, "", UNKNOWN_BRAND) else value


def ok(data, message="", status=None):
    if status is None:
        status = ("confirmation_required"
                  if isinstance(data, dict) and data.get("requires_confirmation") else "completed")
    return {"success": True, "status": status, "data": data, "message": message}


def fail(message, data=None, status=None, code=None, field=None, choices=None):
    details = dict(data) if isinstance(data, dict) else ({} if data is None else {"value": data})
    for key, value in (("code", code), ("field", field), ("choices", choices)):
        if value is not None:
            details[key] = value
    status = status or details.get("status") or "failed"
    return {"success": False, "status": status, "data": details or None, "message": message}


# 검색 결과 요약(Toolbox._with_quick_picks). "제일 싼/리뷰 많은/평점 높은" 고르기를 앱이 계산한다.
QUICK_PICK_TOOLS = frozenset({"search_product", "search_by_image_and_text"})
QUICK_PICK_CHEAPEST = 5     # 사이즈 조건("L로")이 있으면 최저가에 그 사이즈가 없을 수 있어 넉넉히
QUICK_PICK_OTHERS = 3
# 성별을 말했다고 볼 수 있는 표현. "남"·"여" 한 글자는 너무 흔해서 넣지 않는다.
GENDER_EVIDENCE = re.compile(r"남성|남자|여성|여자|공용|남녀|성별|상관\s*없|무관|\bmen|\bwomen|unisex",
                             re.IGNORECASE)
GENDER_LABELS = {"남성": "남성용", "여성": "여성용", "공용": "남녀공용", "전체": "성별 무관"}


def search_clarification(name, arguments, previous_gender=None):
    """대화형 상품 탐색의 필수 조건. 카탈로그 화면 검색(/api/products)은 Store 를 직접 써서 거치지 않는다.

    '전체'는 사용자가 제한 없음을 선택한 값이며 생략(아직 모름)과 다르다.
    특정 상품명 조회와 사진 속 품목은 이미 검색 대상이 있으므로 범주를 다시 묻지 않는다.
    previous_gender: 앞 사진 검색에서 쓴 성별. 새 사진이라 다시 묻되 "이번에도 남성용으로?"로 제안한다.
    """
    if name not in {"search_product", "search_by_image_and_text"}:
        return None
    if name == "search_product" and arguments.get("product_name"):
        return None
    missing = []
    if not arguments.get("gender"):
        missing.append("gender")
    if name == "search_product" and not (arguments.get("category") or arguments.get("group")):
        missing.append("category")
    if not missing:
        return None
    questions = []
    if "gender" in missing:
        if previous_gender in GENDER_LABELS:
            questions.append(f"이번에도 {GENDER_LABELS[previous_gender]}으로 찾을까요? "
                             "다른 성별(남성용·여성용·남녀공용)이나 성별 무관으로도 볼 수 있어요.")
        else:
            # 예전 문구 "…중 어떤 상품을 찾으세요?"는 사진 아이템을 고르라는 말로 읽혔다(2026-10-06).
            questions.append("어느 성별의 상품을 찾으세요? 남성용·여성용·남녀공용 중에서 골라 주세요. "
                             "성별 무관으로도 볼 수 있어요.")
    if "category" in missing:
        questions.append("상의·하의·아우터·신발 중 어떤 종류를 원하세요? 종류도 상관없으면 말씀해 주세요.")
    optional = []
    if not arguments.get("color"):
        optional.append("색상")
    if arguments.get("min_price") is None and arguments.get("max_price") is None:
        optional.append("예산")
    if optional:
        questions.append(f"원하시는 {'이나 '.join(optional)}이 있으면 함께 알려 주세요. 없으면 생략하셔도 돼요.")
    return fail("\n".join(questions), {
        "status": "needs_input", "code": "SEARCH_CONTEXT_REQUIRED",
        "missing_fields": missing,
        "choices": {key: ([*GENDER_NAMES, "전체"] if key == "gender"
                          else [*GROUP_NAMES, "전체"]) for key in missing},
        "known_filters": dict(arguments),
    })


# ======================================================================
# 모델에게 알려줄 Tool 스키마
#
# 검색·이미지 검색·비교·장바구니와 주문·취소·반품·결제 도구를 등록합니다.
# 되돌릴 수 없는 4개(remove_from_cart, cancel_order, return_order, buy_from_cart)는
# confirm 없이 부르면 미리보기만 돌려주고, 실행은 화면의 승인 버튼이 합니다.
# ======================================================================

# 되돌릴 수 없는 4개 도구가 공유하는 확인 절차 한 줄. 자세한 규칙은 시스템 프롬프트 7번에 있다
# (도구마다 같은 다섯 문장을 되풀이하던 것을 줄였다 — 매 요청 입력 토큰).
_CONFIRM_RULE = (
    "부르면 확인 문장(미리보기)만 돌아오고 실행은 앱의 승인 버튼이 한다. "
    "확인 질문을 직접 만들지 말고 먼저 이 Tool 을 불러 그 문장을 전하라. ")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_product",
            "description": (
                "상품을 찾는 모든 요청의 첫 단계. 구조화 조건은 전용 인자로 거르고 남은 "
                "용도·기능·착용감은 semantic_query로 정렬한다. "
                "예: '가볍고 편한 남성 운동화' -> category=운동화, gender=남성, "
                "semantic_query='가볍고 편한'. 일반 추천은 성별과 category 또는 group을 먼저 확인한다. "
                "모르는 값은 추측하지 말고 생략해 호출하면 needs_input 질문을 받는다. "
                "'전체'는 사용자가 해당 조건이 상관없다고 명시한 경우에만 쓴다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # enum 을 쓰는 이유:
                    # 사용자가 "블랙"이라고 말해도 모델이 여기 적힌 값 중에서 고르게 됩니다.
                    # 코드에 별칭 표를 두는 것보다 낫습니다 - 유효한 값이 계약에 적혀 있고,
                    # 값이 추가돼도 이 목록 한 곳만 고치면 됩니다.
                    "category": {
                        "type": "string",
                        "enum": CATEGORY_NAMES,
                        "description": "상품 소분류. **사용자가 말한 낱말이 위 목록에 있으면 반드시 이 인자를 쓴다.** "
                            "'비 올 때 걸칠 재킷' -> category=재킷 (group 은 비운다). "
                            "'바지', '청바지', '슬랙스' -> category=팬츠, "
                            "'치마' -> category=스커트. 이때 group=하의는 쓰지 않는다. "
                            "목록에 없는 넓은 말일 때만 group 으로 넘어간다",
                    },
                    "group": {
                        "type": "string",
                        "enum": [*GROUP_NAMES, "전체"],
                        "description": (
                            "대분류. '신발', '겉옷'처럼 category 목록에 없는 넓은 말일 때만 쓴다"
                        ),
                    },
                    "gender": {
                        "type": "string",
                        "enum": [*GENDER_NAMES, "전체"],
                        "description": (
                            "성별 구분. 남성 또는 여성으로 검색하면 남녀공용 상품도 함께 나온다. "
                            "모르면 생략해 질문한다. 사용자가 성별 무관이라고 명시하면 전체. "
                            "남녀공용은 공용 상품만 찾는 것이며 전체와 다르다"
                        ),
                    },
                    "brand": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "description": (
                            "사용자가 말한 브랜드명. 브랜드는 수천 종이라 enum으로 제한하지 "
                            "않는다. 한글로 널리 알려진 해외 브랜드는 공식 영문 표기로 바꾼다. "
                            "예: '타미 힐피거' → 'Tommy Hilfiger'. 브랜드를 말하지 않았으면 생략한다"
                        ),
                    },
                    "product_name": {
                        "type": "string",
                        "description": (
                            "사용자가 특정 상품명을 직접 말했을 때만 넣는다. 상품명 일부도 가능하다. "
                            "예: '메쉬 배색 하이웨이스트 레깅스 찾아줘' -> product_name='메쉬 배색 하이웨이스트 레깅스'. "
                            "품목·용도·분위기를 상품명으로 추측해서 넣지 않는다"
                        ),
                        "minLength": 1,
                        "maxLength": 100,
                    },
                    "max_price": {"type": "integer", "minimum": 0, "description": "이 금액 이하의 상품만. 단위는 원"},
                    "min_price": {"type": "integer", "minimum": 0, "description": "이 금액 이상의 상품만. 단위는 원"},
                    "color": {
                        "type": "string",
                        "enum": COLOR_NAMES,
                        "description": "상품 색상. 사용자가 '블랙' 처럼 말해도 여기 있는 값으로 바꿔서 넣을 것",
                    },
                    "size": size_schema(
                            "이 사이즈의 재고가 있는 상품만. 카테고리마다 체계가 다르다. "
                            "신발은 220~290(mm), 의류는 XS~2XL, 팬츠는 24~38(인치) 또는 S~XL, "
                            "벨트는 85~110(cm) 또는 FREE, 모자·스카프는 FREE, 가방·기타는 ONE_SIZE"
                    ),
                    "material": {
                        "type": "string",
                        "enum": MATERIAL_NAMES,
                        "description": (
                            "소재. 사용자가 '린넨 셔츠', '가죽 부츠' 처럼 소재를 말하면 쓴다. "
                            "'면 100%' 같은 혼용률이 아니라 여기 있는 짧은 이름을 넣을 것"
                        ),
                    },
                    **exclude_schema(),
                    "machine_washable": {
                        "type": "boolean",
                        "description": "true 면 세탁기 사용이 가능한 상품만 검색한다",
                    },
                    "in_stock": {
                        "type": "boolean",
                        "description": (
                            "'재고 있는 것만'처럼 구매 가능한 상품만 원하면 true. 사이즈를 지정하면 "
                            "그 사이즈 재고로 이미 거르므로 생략한다"),
                    },
                    "semantic_query": {
                        "type": "string",
                        "description": (
                            "구조화 인자로 표현할 수 없는 용도·기능·분위기·착용감과 반팔·민소매·크롭 같은 "
                            "외형 조건만 짧게. 예: '비 오는 날 신기 좋은', '격식 있는 출근용', '반팔'. "
                            "구조화 조건은 반복하지 않고 "
                            "category를 대신하지 않는다. 의미 요구가 없으면 생략한다"
                        ),
                        "minLength": 1,
                        "maxLength": 200,
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["price_asc", "price_desc", "rating", "review"],
                        "description": (
                            "price_asc 저가순, price_desc 고가순, rating 평점순('평점'·'별점'을 말했을 때만), "
                            "review 리뷰많은순('리뷰'를 말했을 때만). '편한 순'·'추천순'은 sort가 아니라 "
                            "semantic_query"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_by_image_and_text",
            "description": (
                "첨부 사진을 기준으로 상품을 찾는 유일한 사진 검색 툴. 조건이 없는 '이거랑 "
                "비슷한 거'도 이 툴이다. [검증된 첨부 이미지]의 query_image_id와 analysis_id를 "
                "그대로 넘긴다. retrieval_query_en에는 사진과의 관계를 보존한 영어 검색문을, "
                "구조화 인자에는 사용자가 직접 말한 조건만(근거는 user_quotes) 넣는다. "
                "자세한 규칙은 시스템 프롬프트의 '사진 검색 인자 최종 확인'을 따른다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_image_id": {
                        "type": "string", "minLength": 36, "maxLength": 36,
                        "description": "이미지 업로드 API가 발급한 UUID",
                    },
                    "semantic_query": {
                        "type": "string", "minLength": 1, "maxLength": 200,
                        "description": (
                            "사용자가 말한 분위기·용도(예: '출근용'). 비교 표현('비슷한', '더 밝은')과 "
                            "구조화 조건은 넣지 않는다"
                        ),
                    },
                    "retrieval_query_en": {
                        "type": "string", "minLength": 1, "maxLength": 500,
                        "description": (
                            "사용자 발화에서 뽑은 충실한 영어 시각 검색문. 이미지와의 유사·상대 "
                            "관계를 보존하고 사용자가 말하지 않은 색·스타일을 추가하지 않는다. "
                            "가격·재고·사이즈·브랜드·성별·카테고리는 제외한다"),
                    },
                    "analysis_id": {
                        "type": "string", "minLength": 36, "maxLength": 36,
                        "description": (
                            "[검증된 첨부 이미지]에 적힌 사진 분석 UUID. 있으면 그대로 넘겨 "
                            "사진을 다시 분석하지 않게 한다. 추측하지 않는다"),
                    },
                    "reference_attributes": {
                        "type": "array", "maxItems": 4,
                        "items": {"type": "string", "enum": ["pattern", "length", "silhouette", "details"]},
                        "description": (
                            "선택한 사진 아이템에서 그대로 유지할 외관 축. search_features_en에 있는 "
                            "pattern 무늬, length 기장, silhouette 형태·핏, details 구조만 고른다. "
                            "사용자가 바꾸거나 제외하거나 상관없다고 한 축은 반드시 뺀다. "
                            "예: '무지로 더 짧게'는 pattern·length 제외. 전부 무관하면 []. "
                            "서버가 고른 축의 근거 있는 영어 설명을 검색문에 붙이므로 직접 복사하지 않는다"),
                    },
                    "item_id": {
                        "type": "string", "minLength": 6, "maxLength": 20,
                        "description": (
                            "사진에 아이템이 여러 개라 사용자가 하나를 고른 경우 그 item_1 같은 "
                            "ID. analysis_id 없이 사용하지 않음"),
                    },
                    "group": {"type": "string", "enum": GROUP_NAMES,
                              "description": "사용자가 넓은 대분류만 말했을 때"},
                    "category": {"type": "string", "enum": CATEGORY_NAMES,
                                 "description": "사용자가 말한 정확한 소분류"},
                    "gender": {"type": "string", "enum": [*GENDER_NAMES, "전체"],
                               "description": "사용자에게 확인한 상품 성별. 성별 무관이면 전체, 모르면 생략"},
                    "brand": {
                        "type": "string", "minLength": 1, "maxLength": 100,
                        "description": "사용자가 직접 말한 브랜드. 언급하지 않으면 생략",
                    },
                    "min_price": {"type": "integer", "minimum": 0},
                    "max_price": {"type": "integer", "minimum": 0},
                    "color": {
                        "type": "string", "enum": COLOR_NAMES,
                        "description": (
                            "사용자가 직접 말한 색상을 DB 표준값으로 변환. 사진에서 본 색은 넣지 않는다"),
                    },
                    "size": size_schema("사용자가 직접 말한 사이즈의 재고가 있는 상품만"),
                    "material": {
                        "type": "string", "enum": MATERIAL_NAMES,
                        "description": "사용자가 직접 말한 소재. 사진에서 본 소재는 넣지 않는다",
                    },
                    **exclude_schema(),
                    "user_quotes": {
                        "type": "object",
                        "description": (
                            "category·color·material 값의 근거가 된 사용자 원문 표현을 그대로 옮긴다. "
                            "서버가 이 표현이 사용자 문장에 실제로 있는지 확인해 하드 필터로 쓴다"),
                        "properties": {
                            "category": {"type": "string", "minLength": 1, "maxLength": 30},
                            "color": {"type": "string", "minLength": 1, "maxLength": 30},
                            "material": {"type": "string", "minLength": 1, "maxLength": 30},
                        },
                    },
                    "machine_washable": {"type": "boolean"},
                    "in_stock": {
                        "type": "boolean",
                        "description": "구매 가능한 재고를 요구하면 true. 품절도 보고 싶으면 생략",
                    },
                },
                "required": ["query_image_id", "retrieval_query_en"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_info",
            "description": (
                "상품 하나의 상세 정보를 조회한다. 가격, 색상, 사이즈별 재고 수량, 평점, "
                "소재, 세탁 방법, 세탁기 사용 가능 여부, 배송 소요일을 반환한다. "
                "특정 사이즈의 재고를 확인하거나 세탁·관리 방법을 물어볼 때 사용한다. "
                "product_id 를 모르면 product_name 에 상품명을 넣어도 된다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "조회할 상품 ID. 검색 결과의 product_id 그대로. 예: AF-B00TEST123"},
                    "product_name": {
                        "type": "string",
                        "description": "상품명. product_id 를 모를 때 대신 사용한다. 예: 미드카프 밀리터리 컴뱃 부츠",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comparing_info",
            "description": (
                "여러 상품의 가격, 평점, 리뷰 수, 소재, 세탁기 사용 가능 여부, 배송일을 "
                "나란히 비교할 수 있는 표를 반환한다. "
                "이 Tool 은 상품을 추천하거나 선택하지 않는다. "
                "어떤 상품이 사용자 조건에 가장 적합한지는 반환된 정보를 보고 직접 판단해야 한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_COMPARE_ITEMS,
                        "description": "비교할 상품 ID 목록. search_product 결과에서 고른다",
                    },
                },
                "required": ["product_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": (
                "상품을 장바구니에 담는다. 재고가 부족하면 실패하며 남은 수량을 알려준다. "
                "상품 ID 를 모르면 product_name 에 상품명을 넣어라. ID 를 추측하지 마라. "
                "**색상과 사이즈를 둘 다 지정해야 담긴다.** 사용자가 말하지 않았다면 "
                "추측하지 말고 생략해서 호출하라 — 선택지가 돌아오므로 그걸로 되물어라. "
                "색이 하나뿐인 상품은 color 를 생략해도 담긴다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "담을 상품 ID. 검색 결과의 product_id 그대로. 예: AF-B00TEST123"},
                    "product_name": {
                        "type": "string",
                        "description": "상품명. ID 를 모를 때 대신 사용한다. ID 를 추측하지 말 것",
                    },
                    "color": {
                        "type": "string",
                        "enum": COLOR_NAMES,
                        "description": "색상. 모르면 생략할 것 (추측 금지)",
                    },
                    "size": size_schema("사용자가 말한 사이즈 그대로(66→L 처럼 바꾸지 말 것). "
                                        "모르면 생략 (추측 금지)"),
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 99,
                                 "description": "수량. 1 이상의 정수. 기본 1"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_cart",
            "description": (
                "현재 장바구니에 담긴 상품 목록과 합계 금액을 반환한다. "
                "장바구니 내용을 묻는 질문에 답할 때, 그리고 결제하기 전에 "
                "무엇을 구매하는지 사용자에게 확인시킬 때 사용한다."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_from_cart",
            "description": (
                "장바구니에서 상품을 빼거나 수량을 줄인다. " + _CONFIRM_RULE +
                "상품 ID 를 모르면 product_name 에 상품명을 넣어라. ID 를 추측하지 마라. "
                "size 를 생략하면 그 상품을 사이즈 상관없이 전부 뺀다. "
                "quantity 를 지정하면 그 수량만큼만 줄인다. "
                "대상이 여럿이거나 '225는 3개 다 빼고 230은 2개만' 처럼 사이즈마다 수량이 다르면 "
                "두 번 나눠 부르지 말고 items 배열에 담아 한 번만 호출한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "뺄 상품 ID. 장바구니의 product_id 그대로. 예: AF-B00TEST123"},
                    "product_name": {
                        "type": "string",
                        "description": "상품명. ID 를 모를 때 대신 사용한다. ID 를 추측하지 말 것",
                    },
                    "color": {"type": "string", "enum": COLOR_NAMES,
                              "description": "특정 색상만 뺄 경우 지정. 생략하면 색 상관없이"},
                    "size": size_schema("특정 사이즈만 뺄 경우 지정"),
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 99,
                                 "description": "줄일 수량. 1 이상의 정수. 생략하면 해당 항목 전부"},
                    "items": {
                        "type": "array",
                        "maxItems": 10,
                        "minItems": 1,
                        "description": (
                            "여러 대상을 한 번에 뺄 때 사용한다. "
                            "이 인자를 쓰면 product_id/product_name/size/quantity 는 넣지 않는다"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string", "description": "뺄 상품 ID"},
                                "product_name": {"type": "string",
                                                 "description": "상품명. ID 를 모를 때"},
                                "color": {"type": "string", "enum": COLOR_NAMES,
                                          "description": "색상"},
                                "size": size_schema("사이즈"),
                                "quantity": {"type": "integer", "minimum": 1, "maximum": 99,
                                             "description": "줄일 수량. 생략하면 해당 항목 전부"},
                            },
                        },
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "앱 내부 실행용입니다. **모델은 이 값을 넣지 마세요.** "
                            "넣어도 실행되지 않고 미리보기만 돌아옵니다. "
                            "실행은 사용자가 화면의 승인 버튼을 눌러야 일어납니다."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    # ------------------------------------------------------------------
    # 주문 관련 Tool
    #
    #   search_order      조건(기간, 상품명, 상태)으로 주문 목록 검색
    #   get_order         주문 ID 로 상세 조회 (배송 상태 포함)
    #   cancel_possible   취소 가능 여부 + 이유 + 대안
    #   cancel_order      실제 취소 (미리보기 -> 승인 버튼)
    #   return_possible   반품 가능 여부 + 남은 기간
    #   return_order      실제 반품 신청 (미리보기 -> 승인 버튼)
    #   buy_from_cart     장바구니 결제 (미리보기 -> 승인 버튼)
    # ------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "search_order",
            "description": (
                "주문 내역을 검색한다. 취소·반품·배송 조회의 첫 단계로 사용한다. "
                "주문 ID 를 모를 때 여기서 찾는다. 주문 ID 를 추측하지 마라. "
                "날짜 조건이 세 가지이므로 사용자가 말한 표현에 맞는 것을 골라야 한다. "
                "'어제 주문한' -> ordered_days_ago=1 (오늘 주문한 것이 섞이면 안 된다), "
                "'최근 며칠 안에 주문한' -> ordered_within_days, "
                "'지난주에 받은' -> delivered_within_days=7 (주문일이 아니라 수령일 기준), "
                "'8월 주문' -> ordered_from/ordered_to 로 그 달의 첫날과 마지막날을 지정."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "상품명이나 카테고리. 예: 운동화, 셔츠, 컴뱃 부츠",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["배송 준비 중", "배송 중", "배송 완료", "취소됨", "반품 신청됨"],
                        "description": "주문 상태로 거른다",
                    },
                    "ordered_within_days": {
                        "type": "integer", "minimum": 0, "maximum": 365,
                        "description": "주문일이 오늘로부터 N일 이내. 오늘 주문한 것도 포함된다",
                    },
                    "ordered_days_ago": {
                        "type": "integer", "minimum": 0, "maximum": 365,
                        "description": (
                            "정확히 N일 전에 주문한 것만. '어제' 는 1, '오늘' 은 0. "
                            "'어제 주문한' 처럼 특정 날짜를 말하면 이것을 쓴다"
                        ),
                    },
                    "delivered_within_days": {
                        "type": "integer", "minimum": 0, "maximum": 365,
                        "description": (
                            "수령일이 오늘로부터 N일 이내. '지난주에 받은' 은 7. "
                            "아직 받지 않은 주문은 걸리지 않는다"
                        ),
                    },
                    "ordered_from": {
                        "type": "string",
                        "description": (
                            "이 날짜 이후에 주문한 것만. YYYY-MM-DD 형식. "
                            "'8월 주문 내역' 처럼 달로 끊어 볼 때 ordered_to 와 함께 쓴다"
                        ),
                    },
                    "ordered_to": {
                        "type": "string",
                        "description": "이 날짜 이전에 주문한 것만. YYYY-MM-DD 형식",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": (
                "주문 하나의 상세 정보를 조회한다. 주문일, 발송일, 수령일, 상태, "
                "취소 가능 여부, 반품 가능 여부와 반품 마감일을 함께 반환한다. "
                "order_id 를 모르면 search_order 로 먼저 찾아라."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "주문 ID. 예: ORD-1001"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_possible",
            "description": (
                "이 주문을 지금 취소할 수 있는지 확인한다. 주문 상태는 바뀌지 않는다. "
                "취소할 수 없으면 그 이유와 대신 할 수 있는 방법을 함께 반환한다. "
                "사용자가 '취소되나요?' 처럼 실행 없이 물어볼 때만 쓴다. "
                "취소할 의사가 분명하면 이 Tool 을 건너뛰고 cancel_order 를 바로 불러라. "
                "cancel_order 가 가능 여부를 스스로 확인하므로 먼저 물어볼 필요가 없고, "
                "여기서 한 번 묻고 다시 확인받으면 사용자가 같은 답을 두 번 하게 된다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "주문 ID"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": (
                "주문을 취소한다. " + _CONFIRM_RULE +
                "가능 여부는 이 Tool 이 스스로 확인하므로 cancel_possible 을 먼저 부를 필요가 없다. "
                "취소가 불가능한 주문이면 이유와 대안을 반환한다. "
                "한 번에 한 건만 취소할 수 있다. 여러 건이면 하나씩 확인받아라."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "취소할 주문 ID"},
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "앱 내부 실행용입니다. **모델은 이 값을 넣지 마세요.** "
                            "넣어도 실행되지 않고 미리보기만 돌아옵니다. "
                            "실행은 사용자가 화면의 승인 버튼을 눌러야 일어납니다."
                        ),
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_possible",
            "description": (
                "이 주문을 반품할 수 있는지 확인한다. 주문 상태는 바뀌지 않는다. "
                "반품 가능 기간은 수령일로부터 계산되며, 마감일도 함께 반환한다. "
                "사용자가 '환불되나요?' 처럼 실행 없이 물어볼 때만 쓴다. "
                "반품할 의사가 분명하면 이 Tool 을 건너뛰고 return_order 를 바로 불러라. "
                "return_order 가 가능 여부를 스스로 확인한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "주문 ID"},
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "return_order",
            "description": (
                "반품을 신청한다. " + _CONFIRM_RULE +
                "가능 여부는 이 Tool 이 스스로 확인하므로 return_possible 을 먼저 부를 필요가 없다. "
                "이 Tool 은 반품 '접수' 까지만 한다. 환불은 상품 회수가 끝난 뒤에 처리되므로 "
                "사용자에게 '환불되었습니다' 가 아니라 '반품이 접수되었고 회수 후 환불된다' 고 알려야 한다. "
                "한 번에 한 건만 신청할 수 있다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "반품할 주문 ID"},
                    "reason": {
                        "type": "string",
                        "description": "반품 사유. 사용자가 말한 이유를 그대로 적는다. 예: 사이즈가 맞지 않음",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "앱 내부 실행용입니다. **모델은 이 값을 넣지 마세요.** "
                            "넣어도 실행되지 않고 미리보기만 돌아옵니다. "
                            "실행은 사용자가 화면의 승인 버튼을 눌러야 일어납니다."
                        ),
                    },
                },
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buy_from_cart",
            "description": (
                "장바구니에 담긴 상품을 실제로 주문한다(결제). " + _CONFIRM_RULE +
                "사용자가 '주문할게', '결제해줘' 라고 명확히 말했을 때만 사용한다. "
                "장바구니에 담는 것(add_to_cart)과 혼동하지 마라. "
                "장바구니 전체가 아니라 일부만 주문하려면 items 에 그 항목만 담아라. "
                "이때 나머지를 빼려고 remove_from_cart 를 부를 필요가 없다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "maxItems": 10,
                        "minItems": 1,
                        "description": (
                            "장바구니에서 **일부만** 주문할 때 사용한다. "
                            "생략하면 장바구니 전체를 주문한다. "
                            "나머지 항목은 장바구니에 그대로 남는다"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "product_id": {"type": "string",
                                               "description": "주문할 상품 ID"},
                                "product_name": {"type": "string",
                                                 "description": "상품명. ID 를 모를 때"},
                                # 색 목록(enum)을 다시 싣지 않는다(+174 토큰). 결제할 색은 장바구니에
                                # 보이는 값이고, 없는 색이면 store 가 "장바구니에 없습니다" 로 거절한다.
                                "color": {"type": "string", "maxLength": 20,
                                          "description": "특정 색만 주문할 때. 장바구니의 색 이름 그대로"},
                                "size": size_schema("사이즈"),
                                "quantity": {"type": "integer", "minimum": 1,
                                             "maximum": 99,
                                             "description": "주문할 수량. 생략하면 담긴 만큼 전부"},
                            },
                        },
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "앱 내부 실행용입니다. **모델은 이 값을 넣지 마세요.** "
                            "넣어도 실행되지 않고 미리보기만 돌아옵니다. "
                            "실행은 사용자가 화면의 승인 버튼을 눌러야 일어납니다."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
]


# ======================================================================
# 인자 검증
#
# 스키마에 "type": "integer" 라고 적어 두는 것은 모델에게 주는 안내일 뿐,
# 모델이 그대로 보낸다는 보장이 없습니다. 실제로 아래가 전부 통과했습니다.
#
#     remove_from_cart(quantity=-5, confirm=True)   -> 1개가 6개로 늘어남
#     add_to_cart(quantity=1.5)                     -> 1.5개가 담김
#     remove_from_cart(confirm="false")             -> 문자열이 참이라 삭제됨
#
# 스키마를 선언만 하고 검사하지 않으면 선언이 아무 일도 하지 않습니다.
# 그래서 실행 직전에 같은 스키마로 한 번 더 검사합니다.
# 검사에 실패하면 Tool 을 부르지 않으므로 Store 상태는 그대로입니다.
# ======================================================================

# 이름으로 스키마를 찾기 위한 색인. TOOLS 에 등록된 것만 실행 대상입니다.
TOOL_INDEX = {tool["function"]["name"]: tool["function"] for tool in TOOLS}

# 모델에게 보내는 도구 목록. 검증 스키마(TOOLS/TOOL_INDEX)와 달리 앱 전용 인자를 뺀다.
# confirm 은 승인 버튼 뒤 앱이 넣는 값이라 모델에게 보여 줄 이유가 없다(설명만 매 요청 토큰을 먹었다).
# 모델이 그래도 confirm 을 보내면 검증은 통과하고 에이전트가 미리보기로 바꾼다.
_APP_ONLY_ARGUMENTS = ("confirm",)


def _model_facing(tool):
    tool = copy.deepcopy(tool)
    properties = tool["function"]["parameters"].get("properties") or {}
    for name in _APP_ONLY_ARGUMENTS:
        properties.pop(name, None)
    # 모델에게는 유지할 축을 매번 정하게 한다. 실행 검증(TOOLS)은 생략도 받는다(None 은 축을 고르지 않음).
    if tool["function"]["name"] == "search_by_image_and_text":
        tool["function"]["parameters"]["required"].append("reference_attributes")
    return tool


MODEL_TOOLS = [_model_facing(tool) for tool in TOOLS]


def _type_name(value):
    return {bool: "boolean", int: "integer", float: "number",
            str: "string", list: "array", dict: "object"}.get(type(value), type(value).__name__)


def _check_one(key, value, rule):
    """값 하나를 스키마 규칙에 맞춰 검사한다. (정리된 값, 오류메시지) 를 돌려준다."""
    alternatives = rule.get("anyOf")
    if alternatives:
        for alternative in alternatives:
            checked, error = _check_one(key, value, alternative)
            if error is None:
                return checked, None
        return None, (f"'{key}' 는 숫자 사이즈 또는 FREE/S/M/L 같은 문자열이어야 합니다. "
                      f"받은 값: {value!r}")
    expected = rule.get("type")

    # boolean 을 먼저 봅니다. 파이썬에서 bool 은 int 의 하위 타입이라
    # isinstance(True, int) 가 참입니다. 순서를 바꾸면 confirm=1 이 통과합니다.
    if expected == "boolean":
        if not isinstance(value, bool):
            return None, (f"'{key}' 는 true 또는 false 여야 합니다. "
                          f"받은 값: {value!r} ({_type_name(value)}). "
                          '문자열 "true" 나 숫자 1 은 받지 않습니다.')
        return value, None

    if expected == "integer":
        if isinstance(value, bool):
            return None, f"'{key}' 는 정수여야 합니다. true/false 는 받지 않습니다."
        # "270" 처럼 정수만 담긴 문자열은 받아 줍니다. 값이 바뀌지 않는 변환이라
        # 안전하고, 이것까지 막으면 모델이 같은 실수를 반복하며 턴만 소모합니다.
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if not isinstance(value, int):
            return None, (f"'{key}' 는 정수여야 합니다. 받은 값: {value!r} "
                          f"({_type_name(value)}). 1.5 같은 소수는 받지 않습니다.")
    elif expected == "string":
        if not isinstance(value, str):
            return None, f"'{key}' 는 문자열이어야 합니다. 받은 값: {value!r}"
        if "minLength" in rule and len(value) < rule["minLength"]:
            return None, f"'{key}' 는 빈 문자열일 수 없습니다."
        if "maxLength" in rule and len(value) > rule["maxLength"]:
            return None, (f"'{key}' 는 최대 {rule['maxLength']}자까지입니다. "
                          f"받은 길이: {len(value)}")
    elif expected == "array":
        if not isinstance(value, list):
            return None, f"'{key}' 는 배열이어야 합니다. 받은 값: {value!r}"
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            return None, (f"'{key}' 는 한 번에 최대 {rule['maxItems']}개까지입니다. "
                          f"받은 개수: {len(value)}")
        if "minItems" in rule and len(value) < rule["minItems"]:
            return None, f"'{key}' 에 항목이 최소 {rule['minItems']}개 필요합니다."
        item_rule = rule.get("items") or {}
        checked = []
        for index, item in enumerate(value):
            cleaned, error = _check_one(f"{key}[{index}]", item, item_rule)
            if error:
                return None, error
            checked.append(cleaned)
        value = checked

    elif expected == "object":
        # 배열 안의 객체까지 재귀로 검사합니다.
        # 이게 없으면 items=[{"quantity": -5}] 같은 값이 그대로 통과합니다.
        # (바깥 인자만 막고 안쪽을 안 보면 검증이 뚫린 것과 같습니다)
        if not isinstance(value, dict):
            return None, f"'{key}' 는 객체여야 합니다. 받은 값: {value!r}"

        properties = rule.get("properties") or {}
        required = rule.get("required") or []

        missing = [name for name in required if name not in value]
        if missing:
            return None, f"'{key}' 에 필수 항목이 빠졌습니다: {', '.join(missing)}"

        unknown = [name for name in value if name not in properties]
        if properties and unknown:
            return None, (f"'{key}' 에 없는 항목입니다: {', '.join(unknown)}. "
                          f"사용 가능한 항목: {', '.join(properties)}")

        cleaned_object = {}
        for name, inner in value.items():
            if inner is None:
                continue
            result, error = _check_one(f"{key}.{name}", inner, properties.get(name) or {})
            if error:
                return None, error
            cleaned_object[name] = result
        value = cleaned_object

    if "enum" in rule and value not in rule["enum"]:
        return None, (f"'{key}' 에 '{value}' 는 쓸 수 없습니다. "
                      f"가능한 값: {', '.join(map(str, rule['enum']))}")

    if "minimum" in rule and value < rule["minimum"]:
        return None, (f"'{key}' 는 {rule['minimum']} 이상이어야 합니다. "
                      f"받은 값: {value}")
    if "maximum" in rule and value > rule["maximum"]:
        return None, (f"'{key}' 는 {rule['maximum']} 이하여야 합니다. "
                      f"받은 값: {value}")

    return value, None


def validate_call(name, arguments):
    """Tool 이름과 인자를 검사한다. (정리된 인자, 오류메시지) 를 돌려준다.

    오류메시지는 모델이 읽고 스스로 고칠 수 있는 문장으로 씁니다.
    "타입 오류" 가 아니라 "정수여야 합니다. 받은 값: 1.5" 처럼 적습니다.
    """
    if name not in TOOL_INDEX:
        return None, (f"'{name}' 이라는 Tool 은 없습니다. "
                      f"사용 가능한 Tool: {', '.join(TOOL_INDEX)}")

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None, f"'{name}' 의 인자는 객체여야 합니다. 받은 값: {arguments!r}"

    schema = TOOL_INDEX[name].get("parameters") or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [key for key in required if key not in arguments]
    if missing:
        return None, f"'{name}' 에 필수 인자가 빠졌습니다: {', '.join(missing)}"

    unknown = [key for key in arguments if key not in properties]
    if unknown:
        return None, (f"'{name}' 에 없는 인자입니다: {', '.join(unknown)}. "
                      f"사용 가능한 인자: {', '.join(properties) or '없음'}")

    cleaned = {}
    for key, value in arguments.items():
        if value is None:            # 생략과 같게 취급
            continue
        result, error = _check_one(key, value, properties[key])
        if error:
            return None, f"{name}: {error}"
        cleaned[key] = result

    size_error = _normalize_sizes(cleaned)
    if size_error:
        return None, f"{name}: {size_error}"

    if name in {"search_product", "search_by_image_and_text"}:
        # JSON Schema의 minLength는 공백 문자열까지 막지 못하므로 실행 전에 정리한다.
        for key in ("semantic_query", "product_name", "retrieval_query_en"):
            if key in cleaned:
                cleaned[key] = cleaned[key].strip()
                if not cleaned[key]:
                    cleaned.pop(key)

        # "니트" 같은 품목을 상품명에 넣어 필수 조건 확인을 우회하지 않도록 한다.
        if name == "search_product":
            product_name = cleaned.get("product_name")
            if product_name in CATEGORY_NAMES:
                cleaned.setdefault("category", product_name)
                cleaned.pop("product_name")
            elif product_name in GROUP_NAMES:
                cleaned.setdefault("group", product_name)
                cleaned.pop("product_name")
            elif product_name in {"옷", "의류", "상품", "아무거나", "전체"}:
                cleaned.pop("product_name")

        minimum = cleaned.get("min_price")
        maximum = cleaned.get("max_price")
        if minimum is not None and maximum is not None and minimum > maximum:
            return None, (f"{name}: min_price는 max_price보다 클 수 없습니다. "
                          f"받은 범위: {minimum:,}원~{maximum:,}원")

        if cleaned.get("size") is not None and cleaned.get("in_stock") is False:
            return None, (f"{name}: 특정 사이즈 검색은 해당 사이즈 재고가 있는 상품을 "
                          "뜻하므로 in_stock=false와 함께 쓸 수 없습니다.")

        for axis in EXCLUDABLE_AXES:
            value = cleaned.get(axis)
            if value is not None and value == cleaned.get(f"exclude_{axis}"):
                return None, (f"{name}: {axis}와 exclude_{axis}에 같은 값 '{value}'을 넣었습니다. "
                              f"사용자가 원하지 않는 값이면 exclude_{axis}에만 넣으세요.")

        if name == "search_by_image_and_text":
            if cleaned.get("item_id") and not cleaned.get("analysis_id"):
                return None, (f"{name}: item_id는 그 아이템이 속한 analysis_id와 "
                              "함께 넣어야 합니다.")

        if cleaned.get("group") == "전체" and cleaned.get("category"):
            cleaned.pop("group")
        category = cleaned.get("category")
        group = cleaned.get("group")
        if category and group:
            expected_group = next(
                (name for name, categories in _CATALOG_META["category_groups"].items()
                 if category in categories), None)
            if expected_group != group:
                return None, (f"{name}: category='{category}'는 group='{group}'에 "
                              f"속하지 않습니다. 올바른 group은 '{expected_group}'입니다.")
            # 소분류가 더 정확하므로 중복된 대분류는 실행 인자에서 제거한다.
            cleaned.pop("group")

    return cleaned, None


class Toolbox:
    """Tool 실행기. Store 하나를 붙잡고 그 위에서 동작한다.

    store 를 인자로 받는 이유: 사용자(세션)마다 Store 가 따로이므로
    모듈 전역 변수로 두면 사용자들의 장바구니가 뒤섞입니다.

        store = Store()
        toolbox = Toolbox(store)
        toolbox.call("search_product", {"color": "검은색", "max_price": 150000})
    """

    def __init__(self, store: Store):
        self.store = store
        # 이번 턴의 사용자 원문(검증 블록 제외). 에이전트가 턴마다 넣는다.
        # 사진에서 추정한 값이 사용자가 말한 척 하드 필터로 들어오는 것을 걸러내는 기준.
        self.user_text = None
        # 이전 턴을 포함한 사용자 원문(최근 것이 먼저). 있으면 user_text 대신 쓴다 —
        # "검은 니트" 다음 턴 "좀 더 싼 걸로"에서도 검은색은 사용자가 말한 조건이다.
        self.user_texts = None

    # ------------------------------------------------------------------
    # 상품 지정 해석
    #
    # 사용자는 "크롭 슬림 티" 라고 말하지 상품 ID 를 말하지 않습니다.
    # 그래서 모델이 ID 를 지어내는 일이 실제로 자주 일어납니다.
    # (관찰된 사례: 크롭 슬림 티의 ID 를 추측 -> 실패 -> view_cart 로 확인 -> 재시도)
    #
    # 상품을 다루는 모든 Tool 이 이름으로도 찾을 수 있게 해서 그 왕복을 없앱니다.
    # ------------------------------------------------------------------

    def _cart_summary(self):
        """장바구니 현황을 한 줄로 요약한다.

        장바구니를 바꾼 Tool 은 결과 메시지에 이걸 붙입니다.
        data 에도 같은 내용이 들어 있지만, 모델이 message 를 더 확실하게 읽습니다.
        그래야 담기·빼기 직후에 "현재 장바구니는 ..." 을 사용자에게 알려줄 수 있습니다.
        """
        cart = self.store.view_cart()

        if cart["count"] == 0:
            return "현재 장바구니는 비어 있습니다."

        items = " / ".join(
            f"{item['name']} {item.get('color') or ''} {item['size']} 사이즈 "
            f"{item['quantity']}개".replace("  ", " ")
            for item in cart["items"]
        )
        return (f"현재 장바구니: {items} "
                f"(총 {cart['quantity']}개, 합계 {cart['total']:,}원)")

    def _resolve_product(self, product_id=None, product_name=None):
        """product_id 또는 product_name 으로 상품을 찾는다.

        반환: (상품, 오류응답)  - 찾으면 (product, None), 못 찾으면 (None, fail(...))
        """
        if not product_id and not product_name:
            return None, fail("product_id 또는 product_name 중 하나는 필요합니다.",
                              status="invalid_argument", code="PRODUCT_REQUIRED")

        product = self.store.get_product(product_id) if product_id else None
        if product is not None:
            return product, None

        # ID 로 못 찾았으면 이름으로 시도한다.
        # 모델이 이름을 product_id 자리에 넣는 경우도 있어 둘 다 살펴본다.
        candidate, matches = self.store.find_product_by_name(product_name or product_id)
        if candidate is not None:
            return candidate, None

        if matches:
            # 이름만 보여 주면 "그래픽 프린트 반팔 티셔츠" 여섯 개가 나란히 찍힌다.
            # 이름이 같은 상품이 흔해졌으니 브랜드·색·가격으로 갈라 준다.
            def label(p):
                color = next(iter(p.get("colors") or {}), None)
                bits = [p["name"], _brand(p.get("brand")) or "", color or "",
                        f"{p['price']:,}원" if p.get("price") is not None else ""]
                return " · ".join(b for b in bits if b) + f" ({p['id']})"
            names = "; ".join(label(p) for p in matches[:6])
            more = f" 외 {len(matches) - 6}개" if len(matches) > 6 else ""
            return None, fail(
                f"이름이 비슷한 상품이 {len(matches)}개입니다. 하나를 골라 주세요: {names}{more}",
                status="needs_input", code="AMBIGUOUS_PRODUCT", field="product_id",
                choices=[{"product_id": p["id"], "name": p["name"],
                          "brand": _brand(p.get("brand")),
                          "color": next(iter(p.get("colors") or {}), None),
                          "price": p.get("price")} for p in matches[:6]])

        given = product_name or product_id
        return None, fail(
            f"'{given}' 상품을 찾을 수 없습니다. 상품 ID 를 추측하지 마세요. "
            "장바구니 안의 상품이면 view_cart, 그 밖이면 search_product 로 확인하세요.",
            status="no_match", code="PRODUCT_NOT_FOUND")

    # ------------------------------------------------------------------
    # 검색 (글·사진)
    # ------------------------------------------------------------------
    def search_product(self, semantic_query=None, product_name=None,
                       group=None, category=None, gender=None,
                       brand=None, max_price=None, min_price=None, color=None, size=None,
                       material=None, machine_washable=None, in_stock=None, sort=None,
                       exclude_category=None, exclude_color=None, exclude_material=None):
        # 결과 개수는 모델이 결정하지 않는다. 검색 서비스의 안전 상한이다.
        limit = MAX_SEARCH_RESULTS

        requested_brand = brand
        if brand is not None:
            brand = self.store.resolve_brand(brand) or brand

        conditions = dict(
            product_name=product_name, group=group, category=category,
            gender=gender, brand=brand,
            max_price=max_price, min_price=min_price, color=color, size=size,
            material=material, machine_washable=machine_washable, in_stock=in_stock,
        )
        excluded = {axis: value for axis, value in (
            ("category", exclude_category), ("color", exclude_color),
            ("material", exclude_material)) if value is not None}
        negated = self._move_negated_to_excluded(conditions, excluded)
        category, color, material = (
            conditions["category"], conditions["color"], conditions["material"])
        conditions.update({f"exclude_{axis}": value for axis, value in excluded.items()})
        exclusion = {"excluded_filters": excluded} if excluded else {}
        exclusion_note = (
            " 사용자가 원하지 않는다고 한 "
            + ", ".join(f"{axis}={value}" for axis, value in excluded.items())
            + " 옵션이 있는 상품은 결과에서 뺐습니다." if excluded else "")
        if negated:
            exclusion["negated_filters"] = negated
        total = self.store.count_products(**conditions)
        # SQL로 후보를 거른 다음 semantic_query가 있으면 임베딩으로 상위 50개를
        # 고른다. 사용자가 가격·평점·리뷰 정렬을 명시했다면 그 50개 안에서만
        # 다시 정렬한다. 의미 조건이 조용히 사라지지 않게 하기 위한 순서다.
        # 임베딩을 쓸 수 없는 환경에서는 SQL 결과로 안전하게 되돌아간다.
        ranking = "sql"
        semantic = None
        if semantic_query:
            semantic = self.store.search_semantic_result(
                semantic_query, top_k=limit,
                min_score=config.SEMANTIC_MIN_SCORE,
                min_results=config.SEMANTIC_MIN_RESULTS, **conditions)
            products = semantic["products"]
            if semantic["available"]:
                if sort:
                    products = self.store.sort_products(products, sort)
                    ranking = f"semantic_then_{sort}"
                else:
                    ranking = "semantic"
            else:
                products = self.store.search_products(
                    sort=sort, limit=limit, **conditions)
                ranking = "sql_fallback"
        else:
            products = self.store.search_products(
                sort=sort, limit=limit, **conditions)

        # 임베딩 계산은 정상적으로 끝났지만 기준을 넘긴 상품이 없는 경우다.
        # 임베딩 자체가 고장 난 경우의 SQL fallback과 구분해야 임계값이 무력화되지 않는다.
        if semantic is not None and semantic["available"] and not products:
            return ok({
                "total": total,
                "qualified": 0,
                "shown": 0,
                "ranking": ranking,
                "semantic_min_score": config.SEMANTIC_MIN_SCORE,
                "products": [],
            }, (f"구조화 조건에는 상품 {total}개가 있지만 의미 유사도 기준 "
                f"{config.SEMANTIC_MIN_SCORE:.2f} 이상인 상품은 없습니다. "
                "semantic_query를 제거해 조건에 덜 맞는 상품을 다시 검색하지 마세요. "
                "조건을 완화하려면 먼저 사용자에게 물어보세요."), status="no_match")

        if not products:
            # 그냥 "없습니다" 로 끝내지 않고 유효한 값을 함께 알려줍니다.
            # 모델이 잘못된 색상/카테고리를 넣었다면 이걸 읽고 스스로 다시 검색합니다.
            # 코드에 별칭 표를 두는 것보다 이 방식이 낫습니다 — 미리 상상한 오타만이 아니라
            # 모든 종류의 어긋남을 처리하기 때문입니다.
            hints = []
            if color and color not in self.store.available_colors():
                hints.append(
                    f"'{color}' 은 없는 색상입니다. "
                    f"가능한 색상: {', '.join(self.store.available_colors())}")
            if category and category not in self.store.available_categories():
                hints.append(
                    f"'{category}' 는 없는 카테고리입니다. "
                    f"가능한 카테고리: {', '.join(self.store.available_categories())}")
            if requested_brand and self.store.resolve_brand(requested_brand) is None:
                hints.append(
                    f"'{requested_brand}' 브랜드를 찾지 못했습니다. "
                    "공식 영문 표기를 확인하거나 브랜드 조건을 빼고 다시 검색하세요")
            if size is not None:
                valid = self.store.available_sizes(category, gender)
                if valid and size not in valid:
                    hints.append(
                        f"{size} 는 이 품목에 없는 사이즈입니다. "
                        f"가능한 사이즈: {', '.join(map(str, valid))}")

            # 잘못된 값(없는 색상·브랜드 등)은 인자 오류라 실패로 돌려 모델이 고치게 한다.
            # 값은 맞는데 결과만 없는 것은 정상 답(0건)이다. 실패로 두면 모델이 Tool 고장으로
            # 읽고 조건을 멋대로 지우며 재시도한다.
            if hints:
                return fail(" / ".join(hints), status="invalid_argument",
                            code="INVALID_FILTER_VALUE")
            return ok({"total": 0, "qualified": 0, "shown": 0, "ranking": ranking,
                       "products": [], **exclusion},
                      "조건에 맞는 상품이 0건입니다. 조건을 멋대로 빼지 말고, 넓힐지(가격 범위·사이즈 등) "
                      "사용자에게 물어보세요." + exclusion_note, status="no_match")

        # 품절 상품은 빼지 않고 맨 뒤로. 채팅 상위 10개와 화면 그리드가 이 순서를 따른다.
        products = self.store.in_stock_first(products)
        # 모델에게는 필요한 필드만 넘깁니다. dict 를 통째로 주면 토큰이 낭비되고
        # 모델이 엉뚱한 필드에 주목하기도 합니다.
        summaries = [
            {
                "product_id": product["id"],
                "name": product["name"],
                "category": product["category"],
                "gender": product["gender"],
                "brand": _brand(product.get("brand")),
                "price": product["price"],
                "colors": list(product["colors"]),   # 고를 수 있는 색
                "material": product["material"],
                "rating": product["rating"],
                "available_sizes": [s for s, stock in product["sizes"].items() if stock > 0],
            }
            for product in products
        ]
        # "이게 전부인지 일부인지" 를 반드시 알려준다.
        # 없으면 모델이 "운동화는 5종류 있습니다" 처럼 잘못 답한다.
        qualified = semantic["qualified_count"] if semantic and semantic["available"] else total
        backfilled = semantic.get("backfilled", 0) if semantic and semantic["available"] else 0
        if semantic and semantic["available"]:
            if backfilled:
                # 기준 통과가 적어 아래에서 채운 경우. 모델이 "N개를 찾았다" 고만 말하면
                # 사용자는 전부 딱 맞는 상품인 줄 안다. 그렇지 않다고 알려야 한다.
                fit = (f"의미 조건에 잘 맞는 상품은 {qualified}개뿐이어서" if qualified
                       else "의미 조건에 잘 맞는 상품이 없어서")
                tell = (f"딱 맞는 상품은 {qualified}개이고 비슷한 상품도 함께 표시했다" if qualified
                        else "딱 맞는 상품은 없어서 비슷한 상품을 표시했다")
                message = (f"검색 결과 화면에 상품 {len(summaries)}개를 표시합니다. "
                           f"{fit} 비슷한 상품 {backfilled}개를 함께 보여줍니다. "
                           f"사용자에게 '{tell}' 고 안내하세요.")
            elif qualified > len(summaries):
                message = (f"검색 결과 화면에 상품 {len(summaries)}개를 표시합니다. "
                           f"구조화 조건 후보는 {total}개, 의미 유사도 기준을 "
                           f"통과한 후보는 {qualified}개이며 화면 표시 상한은 "
                           f"{MAX_SEARCH_RESULTS}개입니다.")
            else:
                message = (f"검색 결과 화면에 상품 {len(summaries)}개를 표시합니다. "
                           f"의미 유사도 기준을 통과한 상품을 모두 표시했습니다.")
        elif total > len(summaries):
            message = (f"조건에 맞는 상품 {total}개 중 상위 "
                       f"{len(summaries)}개를 검색 결과로 보냅니다.")
        else:
            message = f"조건에 맞는 상품 {total}개를 모두 찾았습니다."

        return ok({
            "total": total,
            "qualified": qualified,
            "shown": len(summaries),
            "displayed": len(summaries),
            "ranking": ranking,
            "backfilled": backfilled,
            "semantic_min_score": (
                config.SEMANTIC_MIN_SCORE
                if semantic and semantic["available"] else None
            ),
            **exclusion,
            "products": summaries,
        }, message + exclusion_note)

    def _resolve_filters(self, arguments, quotes=None):
        """사용자가 말한 값만 하드 필터로 남긴다. 판정 규칙은 filter_resolution 한 곳에 있다.

        검증 블록에 "니트 / 초록색" 요약이 보이면 모델은 color=초록색을 넣고 싶어진다.
        그 값이 하드 필터로 들어가면 VLM이 틀렸을 때 SigLIP 결과까지 함께 잘린다.
        근거가 없는 값은 완화 가능한 기준 필터로, 사용자가 부정한 값은 버린다.
        원문(user_texts)이 비어 있으면 아무것도 바꾸지 않는다.
        """
        import filter_resolution

        return filter_resolution.resolve(arguments, user_texts=self.user_texts, quotes=quotes)

    def _move_negated_to_excluded(self, conditions, excluded):
        """원문에서 부정된 category·color·material을 긍정 필터에서 제외 필터로 옮긴다.

        모델이 "검은색 말고"를 exclude_color 대신 color=검은색으로 넣어도 결과가
        정반대가 되지 않게 하는 안전장치다. 글 검색에는 사진 추정값이 없으므로
        근거 없는 값(soft)은 그대로 긍정 필터로 둔다. 옮긴 값을 돌려준다.
        """
        axes = {axis: conditions[axis] for axis in EXCLUDABLE_AXES
                if conditions.get(axis) is not None}
        negated = self._resolve_filters(axes).dropped if axes else {}
        for axis, value in negated.items():
            conditions[axis] = None
            excluded.setdefault(axis, value)
        return negated

    def search_by_image_and_text(
            self, query_image_id, semantic_query=None, retrieval_query_en=None,
            reference_attributes=None,
            analysis_id=None, item_id=None,
            group=None, category=None,
            gender=None, brand=None, min_price=None, max_price=None, color=None,
            size=None, material=None, machine_washable=None, in_stock=None,
            user_quotes=None, exclude_category=None, exclude_color=None,
            exclude_material=None):
        """사진 검색 단일 도구. 경로 선택·폴백은 retrieval 모듈이 정한다(기본 Qwen3-VL fused).

        1) semantic_query에서 '비슷한' 같은 연산자 표현을 걷어낸다.
        2) category/material/color는 user_quotes와 원문으로 판정한다(filter_resolution).
           사용자가 말한 값은 하드 필터, 근거 없는 값은 기준 필터, 부정한 값은 제외 필터
           (exclude_*)다. 모델이 exclude_*로 직접 넘긴 값도 같은 제외 필터가 된다.
           두 경로(통합 임베딩·RRF 폴백) 모두 같은 판정 결과를 쓴다.
        3) 서버가 먼저 만든 analysis_id가 있으면 재사용하고, 없으면 여기서 분석한다(캐시 우선).
           VLM이 죽어도 SigLIP+텍스트로 계속한다.
        4) 기준 사진의 종류(기본) 안에서 찾고, 결과가 모자라면 서버가 필터를 푼다.
        """
        import image_query_service
        import multimodal_query
        import multimodal_search
        import shopping_image_analysis
        import visual_search_query

        semantic_query, _ = multimodal_query.strip_similarity_operators(semantic_query)
        # search_product 와 같이 브랜드 표기("나이키", "nike")를 DB 표기로 맞춘다.
        requested_brand = brand
        if brand is not None:
            brand = self.store.resolve_brand(brand) or brand
        brand_unknown = bool(requested_brand) and self.store.resolve_brand(requested_brand) is None
        brand_hint = (f"'{requested_brand}' 브랜드를 찾지 못했습니다. "
                      "공식 영문 표기를 확인하거나 브랜드 조건을 빼고 다시 검색하세요.")
        hard = {"group": group, "category": category, "gender": gender, "brand": brand,
                "min_price": min_price, "max_price": max_price, "color": color,
                "size": size, "material": material,
                "machine_washable": machine_washable, "in_stock": in_stock}
        resolution = self._resolve_filters(hard, quotes=user_quotes)
        hard, demoted, negated = resolution.hard, resolution.soft, resolution.dropped
        # 부정한 값을 버리기만 하면 "검은색 말고"에 검은색 상품이 그대로 섞인다. 결과에서 뺀다.
        excluded = {axis: value for axis, value in (
            ("category", exclude_category), ("color", exclude_color),
            ("material", exclude_material)) if value is not None}
        for axis, value in negated.items():
            excluded.setdefault(axis, value)
        exclude_filters = {f"exclude_{axis}": value for axis, value in excluded.items()}
        refused_note = (
            "사용자가 원하지 않는다고 한 "
            + ", ".join(f"{axis}={value}" for axis, value in excluded.items())
            + " 옵션이 있는 상품은 결과에서 뺐습니다." if excluded else None)
        soft_filters = dict(demoted)
        fallback_warning = None
        try:
            resolved = shopping_image_analysis.resolve_search_item(
                self.store.user_id, query_image_id, analysis_id=analysis_id,
                item_id=item_id, category=hard.get("category"),
                group_categories=(None if hard.get("category") else
                                  [c for c, g in GROUP_OF_CATEGORY.items() if g == hard.get("group")]))
            if resolved["ambiguous"]:
                return fail(resolved["warning"], {
                    "requires_item_selection": True,
                    "analysis_id": resolved["analysis_id"],
                    "source_query_image_id": resolved["source_query_image_id"],
                    "items": resolved["items"],
                    "relative_applied": False,
                }, status="needs_input", code="ITEM_SELECTION_REQUIRED", field="item_id")
            if resolved.get("no_fashion_item"):
                # 분석은 됐는데 파는 품목이 아니다(마우스 등). 사진만으로 억지 검색하지 않는다.
                seen = ", ".join(resolved.get("other_objects") or [])
                return ok({"shown": 0, "products": [], "code": "NO_FASHION_ITEM",
                           "image_analysis_id": resolved["analysis_id"]},
                          "사진에서 이 쇼핑몰이 파는 패션 아이템(의류·신발·가방·모자·액세서리)을 "
                          "찾지 못했습니다" + (f"(보이는 것: {seen})" if seen else "")
                          + ". 비슷한 상품을 찾지 말고, 이 품목은 취급하지 않는다고 안내하세요.",
                          status="no_match")
            visual_item = resolved["item"]
            visual_source = ("vlm_inferred" if visual_item
                             else "unavailable" if resolved["warning"] else None)
            backend = retrieval.multimodal_backend()
            if backend is not None:
                if not retrieval_query_en or not retrieval_query_en.strip():
                    return fail(
                        f"{backend[0]} 사진 검색에는 사용자의 시각 의도를 보존한 영어 검색문이 필요합니다. "
                        "retrieval_query_en을 만들어 다시 호출해 주세요.")
                # 아이템이 정해졌으면 서버가 크롭 여부를 정한다(면적 CROP_MIN_AREA 이상만).
                query_image = shopping_image_analysis.search_query_image(
                    self.store.user_id, resolved, query_image_id)
                # 사용자가 품목을 말하지 않았으면 사진 아이템의 대분류로 거르고 종류 이름을 검색문에 붙인다.
                photo_group = photo_item_group(visual_item, hard)
                search_query = visual_search_query.build_search_query(
                    retrieval_query_en, visual_item, reference_attributes,
                    name_item=bool(photo_group))
                filters = {**hard, **exclude_filters}
                if photo_group:
                    filters["group"] = photo_group
                photo_group_relaxed = False
                try:
                    # 사용자가 말한 색·소재도 SQL 하드 필터다. 사진 추정값(soft)은 이 경로에서
                    # 쓰지 않고 응답에만 남긴다 — 사진 자체가 질의 벡터에 들어가기 때문이다.
                    found = retrieval.search_by_image(
                        self.store.user_id, query_image["query_image_id"], search_query["query_text"],
                        filters=filters, limit=MAX_SEARCH_RESULTS)
                    if photo_group and not found["products"]:
                        # 사용자가 말한 조건과 겹쳐 0건이면 사진에서 온 대분류만 푼다.
                        filters.pop("group")
                        photo_group_relaxed = True
                        found = retrieval.search_by_image(
                            self.store.user_id, query_image["query_image_id"],
                            search_query["query_text"], filters=filters, limit=MAX_SEARCH_RESULTS)
                except retrieval.BackendUnavailable as error:
                    fallback_warning = f"{error} 기존 검색으로 전환했습니다."   # 짧은 문장만(원인은 서버 로그)
                else:
                    # 모델에게 돌려줄 결과는 필요한 필드만. 저장소 키는 토큰만 늘린다.
                    products = [{key: row[key] for key in (
                        "product_id", "name", "category", "brand", "price", "rating")}
                        for row in found["products"]]
                    for row in products:
                        row["brand"] = _brand(row["brand"])
                    products = self.store.in_stock_first(
                        self._with_options(products), key="product_id")
                    summary = shopping_image_analysis.summarize_item(visual_item)
                    notes = []
                    if photo_group and not photo_group_relaxed:
                        notes.append(f"사진의 {visual_item.get('category')} 기준으로 {photo_group} 안에서 찾았습니다.")
                    elif photo_group_relaxed:
                        notes.append(f"{photo_group} 안에서는 조건에 맞는 상품이 없어 대분류 제한을 풀었습니다.")
                    if demoted:
                        spoken = ", ".join(f"{key}={value}" for key, value in demoted.items())
                        notes.append(f"사용자가 직접 말하지 않은 {spoken}은(는) 조건으로 쓰지 않고 "
                                     "사진 자체로 반영했습니다.")
                    if refused_note:
                        notes.append(refused_note)
                    if not products and brand_unknown:
                        notes.append(brand_hint)
                    # 큰 아이템으로 자동 진행했을 때 사용자가 다른 아이템으로 바로잡을 수 있게 알린다.
                    # 폴백(RRF) 경로에만 있던 안내라 Qwen 경로 답에는 '모자도 있어요'가 없었다.
                    others = resolved["others"]
                    if others:
                        kinds = "·".join(str(item.get("category") or "기타 아이템") for item in others[:3])
                        notes.append(f"사진에 {kinds}도 있습니다. 그쪽을 찾으시면 말씀해 주세요.")
                    return ok({
                        "shown": len(products), "ranking": found["ranking"],
                        "other_items": [{"item_id": item.get("item_id"),
                                         "category": item.get("category")} for item in others],
                        "route": found["route"],
                        "retrieval_query_en": found["query_text"],
                        "base_retrieval_query_en": search_query["base_query_text"],
                        "reference_features_used": search_query["reference_features_used"],
                        "query_enriched": search_query["query_enriched"],
                        "hard_filters": found["hard_filters"],
                        "unapplied_soft_filters": demoted,
                        "negated_filters": negated,
                        "visual_item": visual_item, "visual_summary": summary or None,
                        "query_image_cropped": query_image["cropped"],
                        "image_analysis_id": resolved["analysis_id"],
                        "analysis_cached": resolved["cached"], "products": products,
                    }, " ".join([
                        f"사진과 시각 검색문을 함께 반영한 상품 {len(products)}개를 찾았습니다.",
                        *notes]))
            result = multimodal_search.search(
                self.store, self.store.user_id, query_image_id, semantic_query,
                visual_item=visual_search_query.item_for_fallback(visual_item, reference_attributes),
                visual_source=visual_source,
                soft_filters=soft_filters, limit=MAX_SEARCH_RESULTS,
                **hard, **exclude_filters)
        except (ValueError, image_query_service.ImageQueryError) as error:
            return fail(str(error))

        products = [{
            "product_id": row["product_id"], "name": row.get("name"),
            "category": row.get("category"), "brand": _brand(row.get("brand")),
            "price": row.get("price"), "rating": row.get("rating"),
        } for row in result["results"]]
        products = self.store.in_stock_first(self._with_options(products), key="product_id")

        # --- 메시지: 무엇을 보고, 무엇을 기준으로, 무엇을 못 했는지 한 줄씩 ---
        notes = []
        summary = shopping_image_analysis.summarize_item(visual_item)
        if summary:
            notes.append(f"사진 이해: {summary}.")
        reference = result["reference_filters"]
        if reference:
            basis = " · ".join(str(value) for value in reference.values())
            notes.append(f"사진의 {basis} 기준으로 찾았습니다.")
        if result["reference_filters_relaxed"]:
            dropped = ", ".join(result["reference_filters_relaxed"])
            notes.append(f"조건에 맞는 상품이 적어 {dropped} 기준은 풀었습니다.")
        if demoted:
            spoken = ", ".join(f"{key}={value}" for key, value in demoted.items())
            notes.append(f"사용자가 직접 말하지 않은 {spoken}은(는) 강제 조건이 아니라 "
                         "기준 조건으로 적용했습니다.")
        if refused_note:
            notes.append(refused_note)
        others = resolved["others"]
        if others:
            kinds = "·".join(str(item.get("category") or "기타 아이템") for item in others[:3])
            notes.append(f"사진에 {kinds}도 있습니다. 그쪽을 찾으시면 말씀해 주세요.")
        if resolved["warning"] and not visual_item:
            notes.append("VLM 사진 설명은 사용할 수 없어 SigLIP과 사용자 텍스트 검색으로 계속했습니다.")
        if fallback_warning:
            notes.append(fallback_warning)
        if not products and brand_unknown:
            notes.append(brand_hint)
        headline = (f"사진과 사용자 조건으로 상품 {len(products)}개를 찾았습니다."
                    if not visual_item
                    else f"사진과 묘사를 함께 반영한 상품 {len(products)}개를 찾았습니다.")
        message = " ".join([headline, *notes])

        return ok({"shown": len(products), "ranking": result["ranking"],
                   "hard_filters": result["hard_filters"],
                   "user_filters": result["user_filters"],
                   "reference_filters": reference,
                   "reference_filters_relaxed": result["reference_filters_relaxed"],
                   "demoted_to_reference": demoted,
                   "negated_filters": negated,
                   "query_plan": result["query_plan"],
                   "relative_applied": result["query_plan"]["relative_applied"],
                   "visual_item": visual_item,
                   "visual_summary": summary or None,
                   "other_items": [{"item_id": item.get("item_id"),
                                    "category": item.get("category")} for item in others],
                   "visual_analysis_available": visual_item is not None,
                   "analysis_cached": resolved["cached"],
                   "image_analysis_id": resolved["analysis_id"],
                   "products": products},
                  message)

    # ------------------------------------------------------------------
    # 상품 조회 · 비교 · 장바구니. 각 메서드는 얇게 두고 판단은 store 가 합니다.
    # ------------------------------------------------------------------

    def get_info(self, product_id=None, product_name=None):
        """상품 상세 정보.

        sizes 를 그대로 넘기는 것이 중요하다. {270: 3, 280: 0} 형태를 받으면
        모델이 "270은 3개 남았고 280은 품절입니다" 라고 답할 수 있다.
        care(세탁법), material(소재) 도 함께 넘긴다. 세탁·관리 질문에 이 필드로 답한다.
        """
        product, error = self._resolve_product(product_id, product_name)
        if error:
            return error

        in_stock = [size for size, stock in product["sizes"].items() if stock > 0]

        return ok(
            {
                "product_id": product["id"],
                "name": product["name"],
                "category": product["category"],
                "gender": product["gender"],
                "brand": _brand(product.get("brand")),
                "price": product["price"],
                # 색마다 재고가 다르다. 담으려면 색을 골라야 하므로 색별로 준다.
                "colors": {c: {s: q for s, q in info["sizes"].items() if q > 0}
                           for c, info in product["colors"].items()},
                "sizes": product["sizes"],          # {사이즈: 색 합계 재고}
                "available_sizes": in_stock,        # 재고 있는 사이즈만 추린 것
                "rating": product["rating"],
                "review_count": product["review_count"],
                "description": product["description"],
                "material": product["material"],              # 짧은 이름 ("린넨")
                "material_detail": product["material_detail"],  # 혼용률 ("린넨 100%")
                "care": product["care"],
                "machine_washable": product["machine_washable"],
                "delivery_days": product["delivery_days"],
            },
            f"{product['name']} 상세 정보입니다.",
        )

    def _with_options(self, rows):
        """사진 검색 결과 줄에 고를 수 있는 색·재고 있는 사이즈를 붙인다 (글 검색 결과와 같은 모양).

        채팅에 적는 상품 목록(agent._search_grid_reply)이 글 검색처럼 "가격 · 색 · 사이즈" 를
        보여 주려면 필요하다.
        """
        for row in rows:
            product = self.store.get_product(row["product_id"])
            if product is not None:
                row["colors"] = list(product["colors"])
                row["available_sizes"] = [s for s, stock in product["sizes"].items() if stock > 0]
        return rows

    def comparing_info(self, product_ids):
        """여러 상품을 나란히 비교할 표를 만든다.

        이 Tool 은 어느 상품이 더 나은지 판단하지 않는다. 판단은 모델의 몫이다.
        (스키마의 description 에도 그렇게 적어두었다)
        """
        # 같은 ID 를 두 번 넣으면 같은 행이 두 번 나와 "두 상품이 같다" 로 읽힌다.
        product_ids = list(dict.fromkeys(product_ids or []))
        if not product_ids:
            return fail("비교할 상품 ID 를 하나 이상 지정하세요.")

        rows = []
        missing = []

        for product_id in product_ids:
            product = self.store.get_product(product_id)
            if product is None:
                missing.append(product_id)
                continue

            rows.append({
                "product_id": product["id"],
                "name": product["name"],
                "brand": _brand(product.get("brand")),
                "price": product["price"],
                "colors": list(product["colors"]),
                "rating": product["rating"],
                "review_count": product["review_count"],
                "delivery_days": product["delivery_days"],
                "material": product["material"],
                "material_detail": product["material_detail"],
                "machine_washable": product["machine_washable"],
                "available_sizes": [s for s, stock in product["sizes"].items() if stock > 0],
            })

        if not rows:
            return fail(f"비교할 상품을 찾을 수 없습니다. (요청한 ID: {', '.join(product_ids)})")

        # 없는 ID 를 조용히 빼지 않고 알려준다.
        # 모델이 ID 를 지어낸 것일 수 있고, 그 사실을 알아야 다시 검색한다.
        message = f"{len(rows)}개 상품의 비교 정보입니다."
        if missing:
            message += f" 다음 ID 는 찾지 못했습니다: {', '.join(missing)}"

        return ok(rows, message)

    def add_to_cart(self, product_id=None, size=None, color=None, quantity=1,
                    product_name=None):
        """장바구니에 담는다.

        store 가 (성공여부, 메시지) 를 돌려주므로 그걸 ok()/fail() 로 옮기기만 한다.
        실패 메시지도 store 가 만들어 둔 것을 그대로 쓴다.
        ("재고가 2개뿐입니다" 같은 문장이 모델의 다음 행동을 결정한다)

        size 가 없으면 임의로 고르지 않고 선택지를 돌려준다.
        모델이 사이즈를 추측해 담으면 사용자가 원하지 않는 상품을 사게 된다.
        remove_from_cart 에서 쓴 것과 같은 "애매하면 되묻는다" 패턴이다.
        """
        product, error = self._resolve_product(product_id, product_name)
        if error:
            return error
        product_id = product["id"]

        # 색을 먼저 정한다. 색에 따라 살 수 있는 사이즈가 다르므로 순서가 중요하다.
        colors = product["colors"]
        if color is None and len(colors) == 1:
            color = next(iter(colors))          # 하나뿐이면 물을 것이 없다
        # 색별 재고 사이즈. 색을 묻거나 없는 색을 알릴 때 choices 로 함께 준다.
        color_choices = [{"color": name,
                          "in_stock_sizes": [s for s, q in info["sizes"].items() if q > 0]}
                         for name, info in colors.items()]
        if color is not None and color not in colors:
            return fail(
                f"{product['name']}에 '{color}' 색상은 없습니다. "
                f"가능한 색상: {', '.join(colors)}",
                {"product_id": product_id}, status="needs_input", code="COLOR_UNAVAILABLE",
                field="color", choices=color_choices)

        if color is None:
            # 어느 색에 재고가 있는지까지 알려 준다. 품절인 색을 고르게 두면
            # 되묻기가 한 번 더 늘어난다.
            options = [f"{row['color']}({', '.join(map(str, row['in_stock_sizes']))})"
                       if row["in_stock_sizes"] else f"{row['color']}(품절)"
                       for row in color_choices]
            return fail(
                f"{product['name']} 색상을 지정해 주세요. "
                f"색상별 재고 있는 사이즈: {' / '.join(options)}",
                {"product_id": product_id}, status="needs_input", code="COLOR_REQUIRED",
                field="color", choices=color_choices)

        # 사이즈가 하나뿐인 상품(ONE_SIZE·FREE)은 물을 것이 없다. 재고가 하나만 남은
        # 여러 사이즈 상품은 고르지 않는다 — 그건 사용자 사이즈를 추측하는 일이다.
        if size is None and len(colors[color]["sizes"]) == 1:
            size = next(iter(colors[color]["sizes"]))
        if size is None:
            in_stock = [s for s, q in colors[color]["sizes"].items() if q > 0]
            if not in_stock:
                other = [c for c in self.store.colors_in_stock_any(product_id)
                         if c != color]
                hint = f" {', '.join(other)} 색상에는 재고가 있습니다." if other else ""
                return fail(f"{product['name']} {color}은 전 사이즈 품절입니다.{hint}",
                            {"product_id": product_id, "other_colors_in_stock": other},
                            status="blocked", code="OUT_OF_STOCK")
            return fail(
                f"{product['name']} {color}의 사이즈를 지정해 주세요. "
                f"재고 있는 사이즈: {', '.join(map(str, in_stock))}",
                {"product_id": product_id}, status="needs_input", code="SIZE_REQUIRED",
                field="size", choices=in_stock)

        # 요청 사이즈가 이 상품 표기에 없거나 품절이면 담기 전에 여기서 돌려준다.
        # store 의 안내는 품절 사이즈까지 "가능한 사이즈" 로 나열했고, 모델은 그 목록에서
        # 하나를 골라 담았다("레깅스 66" -> L 실패 -> 28 로 담음). 재고 있는 것만 보여 주고,
        # 고르지 말고 물으라고 못박는다. size_unavailable 은 에이전트의 대체 담기 차단이 쓴다.
        stock_by_size = {str(s): q for s, q in colors[color]["sizes"].items()}
        if stock_by_size.get(size, 0) <= 0:
            in_stock = [s for s, q in stock_by_size.items() if q > 0]
            listed = ", ".join(map(str, in_stock)) or "없음(전 사이즈 품절)"
            why = ("품절입니다" if size in stock_by_size
                   else "이 상품의 사이즈 표기에 없습니다")
            return fail(
                f"{product['name']} {color} {size} 사이즈는 {why}. 재고 있는 사이즈: {listed}. "
                "사용자가 말하지 않은 사이즈로 바꿔 담지 말고, 어느 사이즈로 할지 사용자에게 물으세요.",
                {"size_unavailable": True, "product_id": product_id,
                 "requested_size": size, "in_stock_sizes": in_stock},
                status="needs_input", code="SIZE_UNAVAILABLE", field="size", choices=in_stock)

        success, message = self.store.add_to_cart(product_id, size, color, quantity)

        if not success:
            # 재고보다 많이 담으려 한 경우 등. store 의 문장이 남은 수량을 알려 준다.
            return fail(message, {"product_id": product_id}, status="blocked", code="ADD_REJECTED")

        # 성공하면 담긴 뒤의 장바구니 현황을 함께 넘긴다.
        # 모델이 "담았습니다. 현재 장바구니는 ..." 처럼 답할 수 있다.
        return ok(self.store.view_cart(), f"{message} {self._cart_summary()}")

    def view_cart(self):
        """장바구니 조회.

        비어 있는 것은 오류가 아니라 정상 상태이므로 success=True 로 돌려준다.
        fail 로 돌려주면 모델이 뭔가 잘못된 줄 알고 재시도한다.
        """
        cart = self.store.view_cart()

        if cart["count"] == 0:
            return ok(cart, "장바구니가 비어 있습니다.")

        return ok(
            cart,
            f"장바구니에 {cart['count']}종 {cart['quantity']}개, "
            f"합계 {cart['total']:,}원입니다."
        )

    def remove_from_cart(self, product_id=None, size=None, color=None, quantity=None,
                         confirm=False, product_name=None, items=None):
        """장바구니에서 빼거나 수량을 줄인다. 실행 전에 확인을 받는다.

        confirm=False (기본) -> 무엇이 빠질지 미리 보여주고 실행하지 않는다
        confirm=True         -> 실제로 뺀다

        items 로 여러 대상을 한 번에 받는다. 사용자가
        "225는 3개 다 빼고 230은 2개만" 이라고 말하는데 인자가 하나뿐이면
        모델이 표현할 방법이 없어 한쪽을 버리게 된다.

        여러 줄을 처리할 때는 전부 계산한 뒤에 실행한다 (전부 아니면 전무).
        한 줄씩 지우면서 계산하면 앞줄이 뒷줄의 계산을 바꿔서,
        사용자가 승인한 미리보기와 실제 결과가 달라진다.

        삭제는 되돌릴 수 없다. 사용자가 "에어 스텝 화이트 삭제하려고" 라고 했을 때
        세 사이즈 11개를 한꺼번에 지워버리면 사고다.
        그래서 한 번 확인받는다. cancel_order / return_order 도 같은 패턴을 쓴다.

        확인 절차를 store 가 아니라 여기에 둔 이유:
        화면의 ✕ 버튼은 사용자가 직접 누른 것이므로 이미 확인이다.
        오해의 여지가 있는 것은 에이전트가 대화로 지우는 경우뿐이다.
        """
        # items 와 단일 인자를 섞으면 무엇을 지울지 애매해진다.
        # 조용히 한쪽을 무시하면 사용자가 요청한 것과 다른 게 지워지므로 거절한다.
        singles = {"product_id": product_id, "product_name": product_name,
                   "color": color, "size": size, "quantity": quantity}
        given = [key for key, value in singles.items() if value is not None]
        if items and given:
            return fail(f"items 와 {', '.join(given)} 를 함께 쓸 수 없습니다. "
                        "여러 개를 뺄 때는 items 안에 전부 넣으세요.")

        targets = items if items else [singles]

        # 1단계 - 무엇이 빠질지 전부 계산한다. 장바구니는 아직 그대로다.
        #
        # 한 줄씩 지우면서 계산하면 앞줄이 뒷줄의 계산을 바꾼다.
        # 그러면 사용자가 승인한 미리보기와 실제 결과가 달라진다.
        # 그래서 원래 장바구니 기준으로 전부 계산한 뒤에 실행한다.
        merged = {}        # {(product_id, size): 수량}  중복 줄을 합친다
        names = {}
        for index, target in enumerate(targets, 1):
            product, error = self._resolve_product(target.get("product_id"),
                                                   target.get("product_name"))
            if error:
                label = f"{index}번째 항목: " if items else ""
                return {**error, "message": label + error["message"]}

            rows = self.store.preview_removal(product["id"], target.get("size"),
                                              target.get("color"),
                                              target.get("quantity"))
            if not rows:
                label = f"{index}번째 항목({product['name']})은 " if items else ""
                return fail(f"{label}장바구니에 없습니다." if items
                            else "장바구니에 해당 상품이 없습니다.",
                            status="no_match", code="NOT_IN_CART")

            for row in rows:
                # 색까지 키에 넣는다. 같은 상품 같은 사이즈라도 색이 다르면 다른 줄이다.
                key = (row["product_id"], row.get("color"), row["size"])
                merged[key] = merged.get(key, 0) + row["quantity"]
                names[key] = row["name"]

        # 합친 수량이 실제 담긴 수량을 넘지 않는지 확인한다.
        # ("225 2개 빼고 225 1개 더" 처럼 나눠 적으면 합계가 넘칠 수 있다)
        in_cart = {(line["product"]["id"], line.get("color"), line["size"]):
                   line["quantity"] for line in self.store.cart}
        for key, wanted in merged.items():
            have = in_cart.get(key, 0)
            if wanted > have:
                return fail(f"{names[key]} {key[1] or ''} {key[2]} 사이즈는 "
                            f"{have}개만 담겨 있어 {wanted}개를 뺄 수 없습니다.")

        preview = [{"product_id": pid, "name": names[(pid, color, size)],
                    "color": color, "size": size, "quantity": quantity}
                   for (pid, color, size), quantity
                   in sorted(merged.items(), key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2]))]

        detail = ", ".join(
            f"{row['name']} {row['color'] or ''} {row['size']} 사이즈 "
            f"{row['quantity']}개".replace("  ", " ") for row in preview
        )

        if not confirm:
            hint = ""
            if (not items and size is None and color is None
                    and quantity is None and len(preview) > 1):
                hint = " 일부만 빼시려면 색상이나 사이즈를 지정해 주세요."
            # message 는 사용자에게 그대로 전달될 수 있으므로 사람 말투만 담는다.
            # "confirm 을 true 로 호출하라" 같은 지시는 스키마 description 에만 둔다.
            # (메시지에 넣으면 모델이 사용자에게 그대로 읽어주는 일이 생긴다)
            return ok(
                {"requires_confirmation": True, "preview": preview},
                f"{detail}를 장바구니에서 빼시겠어요?{hint}",
            )

        # 2단계 - 실행. 위에서 전부 검사했으므로 여기서 실패하면 안 된다.
        # 그래도 실패하면 중간에 멈추지 말고 사유를 그대로 올린다.
        for row in preview:
            success, message = self.store.remove_from_cart(
                row["product_id"], row["size"], row["color"], row["quantity"])
            if not success:
                return fail(message)

        return ok(self.store.view_cart(),
                  f"{detail}를 장바구니에서 뺐습니다. {self._cart_summary()}")

    # ==================================================================
    # 주문 · 취소 · 반품
    #
    # 판단은 전부 store 가 한다. 여기서는 결과를 옮기기만 한다.
    # can_cancel 이 된다고 했는데 cancel_order 가 거부하는 모순을 막으려면
    # 판단하는 곳이 한 곳이어야 한다.
    # ==================================================================

    def _order_summary(self, order):
        """주문 한 건을 모델에게 넘길 모양으로 줄인다."""
        return {
            "order_id": order["order_id"],
            "product_id": order["product_id"],
            "product_name": order["product_name"],
            "color": order.get("color"),        # "흰색 말고 검은색 주문" 을 가르려면 필요하다
            "size": order["size"],
            "quantity": order["quantity"],
            "price": order["price"],
            "status": order["status"],
            "ordered_at": order["ordered_at"],
            "shipped_at": order["shipped_at"],
            "delivered_at": order["delivered_at"],
        }

    def _require_order(self, order_id):
        """주문을 찾는다. 없으면 (None, 실패결과).

        주문 ID 를 추측하지 못하게 막는 자리다.
        상품과 달리 주문은 이름으로 찾을 수 없으므로 search_order 를 안내한다.
        """
        order = self.store.get_order(order_id)
        if order is None:
            return None, fail(
                f"'{order_id}' 주문을 찾을 수 없습니다. 주문 ID 를 추측하지 마세요. "
                "search_order 로 먼저 주문을 찾아 order_id 를 확인하세요.",
                status="no_match", code="ORDER_NOT_FOUND")
        return order, None

    def _decision_result(self, order, decision, kind):
        """can_cancel / can_return 판정을 Tool 응답으로 옮긴다."""
        data = {
            "order_id": order["order_id"],
            "product_name": order["product_name"],
            "status": order["status"],
            "allowed": decision.allowed,
            "reason": decision.reason,
            "alternative": decision.alternative,
        }
        if kind == "return":
            deadline = self.store.return_deadline(order["order_id"])
            data["return_deadline"] = deadline

        message = decision.reason
        if decision.alternative:
            message += f" {decision.alternative}"
        # 판정 자체는 정상 동작이므로 success=True 로 돌려준다.
        # allowed=False 를 실패로 돌려주면 모델이 Tool 이 고장난 줄 알고 재시도한다.
        return ok(data, message)

    def search_order(self, keyword=None, status=None, ordered_within_days=None,
                     ordered_days_ago=None, delivered_within_days=None,
                     ordered_from=None, ordered_to=None):
        orders = self.store.search_orders(
            keyword=keyword, status=status,
            ordered_within_days=ordered_within_days,
            ordered_days_ago=ordered_days_ago,
            delivered_within_days=delivered_within_days,
            ordered_from=ordered_from, ordered_to=ordered_to,
        )
        if not orders:
            return ok({"total": 0, "orders": []},
                      "조건에 맞는 주문이 0건입니다. 날짜 조건을 넓히거나 keyword 를 빼고 "
                      "다시 찾아볼 수 있습니다. 전체 주문을 보려면 인자 없이 호출하면 됩니다.",
                      status="no_match")

        total = len(orders)
        # 주문이 많은 사용자에게 전체를 넘기면 문맥이 불어난다. 최근 순 상한까지만 보낸다.
        rows = [self._order_summary(order) for order in orders[:MAX_ORDER_RESULTS]]
        detail = " / ".join(
            f"{row['order_id']} {row['product_name']}({row['status']})" for row in rows
        )
        # 여러 건이면 모델이 임의로 하나를 고르지 않게 못박는다.
        # 엉뚱한 주문을 취소하면 되돌릴 수 없다.
        if len(rows) > 1:
            message = (f"주문 {total}건을 찾았습니다: {detail}. "
                       "여러 건이므로 어느 주문인지 사용자에게 확인한 뒤 진행하세요.")
        else:
            message = f"주문 1건을 찾았습니다: {detail}"
        if total > len(rows):
            message += (f" (최근 {len(rows)}건만 보냈습니다. 나머지는 날짜·상태·keyword 로 "
                        "좁혀서 찾으세요.)")
        return ok({"total": total, "orders": rows}, message)

    def get_order(self, order_id):
        order, error = self._require_order(order_id)
        if error:
            return error

        cancel = self.store.can_cancel(order_id)
        returnable = self.store.can_return(order_id)

        data = self._order_summary(order)
        data.update({
            "can_cancel": cancel.allowed,
            "cancel_reason": cancel.reason,
            "cancel_alternative": cancel.alternative,
            "can_return": returnable.allowed,
            "return_reason": returnable.reason,
            "return_deadline": self.store.return_deadline(order_id),
        })
        return ok(data, f"{order_id} {order['product_name']} 주문 상세 정보입니다. "
                        f"현재 상태는 '{order['status']}' 입니다.")

    def cancel_possible(self, order_id):
        order, error = self._require_order(order_id)
        if error:
            return error
        return self._decision_result(order, self.store.can_cancel(order_id), "cancel")

    def return_possible(self, order_id):
        order, error = self._require_order(order_id)
        if error:
            return error
        return self._decision_result(order, self.store.can_return(order_id), "return")

    def cancel_order(self, order_id, confirm=False):
        """주문을 취소한다. 실행 전에 확인을 받는다.

        remove_from_cart 와 같은 confirm 패턴이다. 다만 items 는 두지 않았다.
        "응" 한 번에 주문 여러 건이 취소되는 것은 장바구니와 위험도가 다르다.
        """
        order, error = self._require_order(order_id)
        if error:
            return error

        decision = self.store.can_cancel(order_id)
        if not decision.allowed:
            # 취소할 수 없는 주문은 확인 단계로 갈 이유가 없다.
            message = decision.reason
            if decision.alternative:
                message += f" {decision.alternative}"
            return fail(message, {"order_id": order_id, "alternative": decision.alternative},
                        status="blocked", code="CANCEL_NOT_ALLOWED")

        row = {
            "order_id": order["order_id"],
            "product_id": order["product_id"],
            "name": order["product_name"],
            "color": order.get("color"),
            "size": order["size"],
            "quantity": order["quantity"],
            "price": order["price"],
        }
        detail = (f"{order['order_id']} {order['product_name']} "
                  f"{order.get('color') or ''} {order['size']} 사이즈 "
                  f"{order['quantity']}개 ({order['price']:,}원)").replace("  ", " ")

        if not confirm:
            return ok({"requires_confirmation": True, "preview": [row]},
                      f"{detail} 주문을 취소할까요?")

        success, message = self.store.cancel_order(order_id)
        if not success:
            return fail(message)
        return ok(self._order_summary(self.store.get_order(order_id)), message)

    def return_order(self, order_id, reason=None, confirm=False):
        """반품을 신청한다. 접수까지만 하고 환불은 회수 후에 처리된다."""
        order, error = self._require_order(order_id)
        if error:
            return error

        decision = self.store.can_return(order_id)
        if not decision.allowed:
            message = decision.reason
            if decision.alternative:
                message += f" {decision.alternative}"
            return fail(message, {"order_id": order_id, "alternative": decision.alternative},
                        status="blocked", code="RETURN_NOT_ALLOWED")

        row = {
            "order_id": order["order_id"],
            "product_id": order["product_id"],
            "name": order["product_name"],
            "color": order.get("color"),
            "size": order["size"],
            "quantity": order["quantity"],
            "price": order["price"],
        }
        detail = (f"{order['order_id']} {order['product_name']} "
                  f"{order.get('color') or ''} {order['size']} 사이즈 "
                  f"{order['quantity']}개 ({order['price']:,}원)").replace("  ", " ")

        if not confirm:
            because = f" 사유: {reason}." if reason else ""
            return ok({"requires_confirmation": True, "preview": [row]},
                      f"{detail} 반품을 신청할까요?{because}")

        success, message = self.store.return_order(order_id, reason)
        if not success:
            return fail(message)

        data = self._order_summary(self.store.get_order(order_id))
        data["refund_status"] = "회수 후 환불 예정"
        return ok(data, message)

    def _selection_from(self, items):
        """모델이 준 items 를 store 가 쓰는 선택 목록으로 바꾼다.

        여기서 이름을 ID 로 바꿔 둡니다. store 는 이름을 모르고,
        모델은 ID 를 모를 때가 많기 때문입니다.

        반환: (선택 목록 또는 None, 오류응답 또는 None)
        """
        if not items:
            return None, None

        selection = []
        for row in items:
            product, error = self._resolve_product(row.get("product_id"),
                                                   row.get("product_name"))
            if error:
                return None, error
            picked = {"product_id": product["id"]}
            # 색도 넘긴다. 빼면 같은 상품·같은 사이즈의 검은색·흰색이 함께 결제 대상이 됐다
            # (store 는 색으로 거를 수 있는데 여기서 버리고 있었다. remove_from_cart 는 넘긴다).
            if row.get("color") is not None:
                picked["color"] = row["color"]
            if row.get("size") is not None:
                picked["size"] = row["size"]
            if row.get("quantity") is not None:
                picked["quantity"] = row["quantity"]
            selection.append(picked)
        return selection, None

    def buy_from_cart(self, items=None, confirm=False):
        """장바구니를 주문으로 전환한다(결제). 실행 전에 확인을 받는다.

        items 를 주면 그 항목만 주문하고 나머지는 장바구니에 남깁니다.
        전에는 일부만 사려면 나머지를 먼저 빼야 했는데, 그건 사용자가
        요청하지 않은 상태 변경이라 승인 대상이 하나 늘어납니다.
        """
        selection, error = self._selection_from(items)
        if error:
            return error

        rows, problem = self.store.preview_checkout(selection)
        if problem:
            return fail(problem)

        detail = ", ".join(
            f"{row['name']} {row.get('color') or ''} {row['size']} 사이즈 "
            f"{row['quantity']}개".replace("  ", " ") for row in rows
        )
        total = sum(row["price"] for row in rows)

        if not confirm:
            scope = "장바구니에서 " if selection else ""
            return ok({"requires_confirmation": True, "preview": rows, "total": total},
                      f"{scope}{detail}를 주문합니다. "
                      f"결제 금액은 {total:,}원입니다. 진행할까요?")

        success, message, created = self.store.checkout(selection)
        if not success:
            return fail(message)
        return ok({"orders": [self._order_summary(order) for order in created],
                   "total": total},
                  f"{message} 주문 번호: "
                  + ", ".join(order["order_id"] for order in created))

    # ------------------------------------------------------------------
    # 디스패치
    # ------------------------------------------------------------------

    def _with_quick_picks(self, name, result):
        """검색 결과 전체(최대 50개)에서 최저가·리뷰 많은·평점 높은 상품을 뽑아 data["quick_picks"] 로 붙인다.

        모델은 글 검색 결과를 상위 10개만 받고, 사진 검색 결과에는 사이즈·리뷰 수가 없다. "비슷한 것 중
        제일 싼 거 L" 에서 Gemma 가 50개 중 42위의 31,000원을 놓치고 30위의 33,000원을 "제일 싼 것"이라고
        담았다(2026-10-06). 고르는 계산은 앱이 하고, 모델은 이 목록에서 사이즈만 맞춰 고른다.
        """
        data = result.get("data") if isinstance(result, dict) else None
        if name not in QUICK_PICK_TOOLS or not result.get("success") or not isinstance(data, dict):
            return result
        rows = [row for row in data.get("products") or [] if isinstance(row, dict) and row.get("product_id")]
        if len(rows) < 2:
            return result
        info = []
        for rank, row in enumerate(rows, 1):
            product = self.store.get_product(row["product_id"])
            if product is None:
                continue
            info.append({"rank": rank, "product_id": product["id"], "name": product["name"],
                         "price": product["price"], "review_count": product.get("review_count") or 0,
                         "rating": product.get("rating") or 0,
                         "available_sizes": [size for size, stock in product["sizes"].items() if stock > 0]})

        def pick(key, reverse, count, fields):
            ordered = sorted(info, key=lambda row: ((-row[key] if reverse else row[key]), row["rank"]))
            return [{field: row[field] for field in fields} for row in ordered[:count]]

        base = ("rank", "product_id", "name")
        data["quick_picks"] = {
            "basis": f"이 검색 결과 {len(info)}개 전체(rank=화면 순위)",
            "cheapest": pick("price", False, QUICK_PICK_CHEAPEST, base + ("price", "available_sizes")),
            "most_reviews": pick("review_count", True, QUICK_PICK_OTHERS, base + ("review_count",)),
            "top_rated": pick("rating", True, QUICK_PICK_OTHERS, base + ("rating", "review_count")),
        }
        return result

    def call(self, name, arguments):
        """이름과 인자 dict 로 Tool 을 실행한다.

        모델이 돌려준 tool_call 을 그대로 넘기면 되도록 만든 진입점입니다.
        모델은 없는 Tool 이름이나 이상한 인자를 만들어낼 수 있으므로
        여기서 방어해야 합니다. 예외가 그대로 터지면 대화가 끊깁니다.
        """
        # 1) 등록된 Tool 인지, 인자가 스키마에 맞는지 먼저 검사합니다.
        #    통과하지 못하면 메서드를 부르지 않으므로 Store 는 그대로입니다.
        cleaned, error = validate_call(name, arguments)
        if error:
            return fail(error, status="invalid_argument", code="INVALID_ARGUMENT")

        if self.user_text is not None:
            cleaned = fill_category_from_user(name, cleaned, self.user_texts)
            clarification = search_clarification(name, cleaned)
            if clarification:
                return clarification
        # 전체는 대화 계약의 선택값일 뿐 실제 카탈로그 값이 아니다.
        if name in {"search_product", "search_by_image_and_text"}:
            cleaned = {k: v for k, v in cleaned.items()
                       if not (k in {"gender", "group"} and v == "전체")}

        # 2) validate_call 이 TOOLS 에 등록된 이름만 통과시키므로 같은 이름의 메서드를 부른다.
        method = getattr(self, name)

        # 인자 이름·필수 인자 오류는 부르기 전에 가른다. 메서드 안에서 난 TypeError 까지
        # "인자가 잘못되었다" 로 돌려주면 모델이 멀쩡한 인자를 바꿔 가며 재시도한다.
        try:
            inspect.signature(method).bind(**cleaned)
        except TypeError as error:
            return fail(f"'{name}' 호출 인자가 잘못되었습니다: {error}",
                        status="invalid_argument", code="INVALID_ARGUMENT")

        # Store 연결은 화면 요청(장바구니·결제 버튼)과 이 에이전트 턴이 함께 쓴다. 결제·취소처럼
        # 여러 문장을 한 트랜잭션으로 묶는 Tool 이 도는 동안 화면 쪽 commit 이 끼어들면 그 트랜잭션이
        # 중간에 확정된다. 그래서 Tool 하나를 실행하는 동안만 Store 의 db_lock 을 쥔다 — 모델을
        # 기다리는 수십 초 동안은 쥐지 않으므로 화면 버튼은 길어야 Tool 하나만큼만 기다린다.
        # 끝나면 읽기로 열린 트랜잭션을 닫고 놓는다.
        self.store.db_lock.acquire()
        try:
            return self._with_quick_picks(name, method(**cleaned))
        except Exception as error:  # noqa: BLE001 — 어떤 예외든 대화를 끊지 않는다
            # 원문(테이블명·쿼리·제약조건이 섞일 수 있다)은 서버 로그에만 남기고, 모델에게는
            # 로그를 찾을 번호와 짧은 안내만 준다.
            error_id = uuid.uuid4().hex[:8]
            print(f"[tool:{name}] 오류 {error_id}: {type(error).__name__}: {error}\n"
                  + traceback.format_exc(), file=sys.stderr)
            return fail(f"'{name}' 처리 중 일시적인 오류가 발생했습니다(오류 번호 {error_id}). "
                        "같은 호출을 반복하지 말고, 처리하지 못했다고 사용자에게 알리세요.",
                        {"error_id": error_id}, status="internal_error",
                        code="TOOL_EXECUTION_FAILED")
        finally:
            db.end_read_transaction(self.store.conn)
            self.store.db_lock.release()
