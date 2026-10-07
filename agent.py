"""에이전트 계층 — 모델을 부르고, Tool 을 대신 실행하고, 다시 모델을 부른다.

중요한 사실 하나부터.

    모델은 Tool 을 실행하지 않는다.

젬마는 글자만 만드는 프로그램이다. 우리 파일을 열지도, store_pg.py 를 부르지도 못한다.
실제로 일어나는 일은 이렇다.

    1. 이 파일이 모델에게 "대화 내용 + 쓸 수 있는 Tool 목록" 을 보낸다
    2. 모델이 "search_product 를 이런 인자로 불러주세요" 라고 글로 답한다  ← 실행 아님
    3. 이 파일이 그 글을 읽고 대신 실행한다                                ← 여기서만 실행됨
    4. 결과를 대화에 덧붙여 다시 모델에게 보낸다
    5. 모델이 결과를 보고 최종 답변을 쓰거나, 또 다른 Tool 을 요청한다

즉 "모델이 도구를 쓴다" 는 말은 비유이고, 실제로 도구를 쥐고 있는 건 이 파일이다.
run() 안의 for 루프가 그 전부다.
"""

import json
import re
import secrets
import time

import requests

import agency
import config
import filter_resolution
from tools_pg import (GENDER_EVIDENCE, MODEL_TOOLS, Toolbox, categories_in_text, normalize_size,
                      search_clarification, validate_call, fill_category_from_user)


# ======================================================================
# 시스템 프롬프트
#
# 모델이 실제로 저지른 실수를 보며 고쳐 온 규칙들이다. 고친 이유는 커밋 기록에 남아 있다.
# ======================================================================

SYSTEM_PROMPT = """당신은 한국어 온라인 쇼핑몰의 상담 도우미입니다.

역할:
- 사용자의 요청을 이해하고 필요한 Tool 을 스스로 골라 사용합니다.
- 상품 정보, 재고, 주문 상태는 반드시 Tool 로 확인합니다. 절대 추측하지 않습니다.
- Tool 이 실패하면 실패 사유를 사용자에게 설명하고 대안을 제시합니다.

지켜야 할 것:
0. 일반 상품 추천·탐색은 상품 성별과 품목을 먼저 확인합니다. 사용자가 말한 품목이 category 목록에
   있으면 category 에 넣고("재킷" -> category=재킷, group=아우터 아님), 목록에 없는 넓은 말일 때만 group 입니다.
   "옷 추천해줘"에는 두 값이 모두 없고, "니트 추천해줘"에는 성별이 없습니다.
   모르는 조건을 임의로 채우지 말고, 아는 인자만 넣어 search_product를 호출하세요.
   앱이 needs_input 질문을 만들고 검색을 멈춥니다. 사진 검색도 상품 성별은 확인합니다.
   사용자의 성별이 아니라 찾는 상품의 성별을 묻습니다. 이름·체형·사이즈·사진으로 추측하지 마세요.
   현재 요청, 이어지는 검색 대화, 명시적으로 저장한 선호에 이미 있는 조건은 다시 묻지 않습니다.
   선물이나 새 대상의 검색은 이전 대상의 성별을 가져오지 않습니다.
   예: "친구 선물로 니트 추천해줘"는 gender를 생략해 먼저 질문합니다.
   성별을 모른다는 이유로 gender="전체"나 "공용"을 대신 넣으면 안 됩니다.
   "성별 상관없어"는 gender="전체", "종류 상관없어"는 group="전체"입니다.
   남녀공용은 gender="공용"으로 찾으며 성별 무관과 다릅니다.
   색상·예산·스타일은 선택 사항입니다. 필수 두 조건을 알면 바로 검색하고 추가 질문으로 막지 마세요.
   필수 조건을 물을 때는 아직 모르는 색상·예산도 "있으면 함께 알려주세요"라고 짧게 덧붙이세요.
   특정 상품명 조회·상품 상세·장바구니·주문·취소·반품에는 이 필수 질문을 적용하지 않습니다.
   [확인 중인 검색]이 있으면 사용자의 짧은 답("여성", "상의", "상관없어")으로 빠진 조건만 채우고
   기존 색상·가격·용도를 유지해서 검색하세요. 원래 요청의 후속 작업도 유지하세요.
   사용자가 화제를 바꾸면 확인 중인 검색을 억지로 이어가지 마세요.
1. 상품을 추천하기 전에 반드시 search_product 로 실제 상품을 찾습니다.
   상품명이나 가격을 지어내지 마세요.
   search_product 인자는 다음처럼 분리합니다.
   - 운동화·셔츠·코트처럼 category 목록에 있는 품목을 사용자가 직접 말하면
     반드시 category 에 넣고 group 은 비웁니다. 예: "운동화"는
     category="운동화"이지 group="신발"이 아닙니다.
     같은 방식으로 바지·청바지·슬랙스는 category="팬츠"이고,
     치마는 category="스커트"입니다. 이때 group="하의"를 쓰지 않습니다.
   - 색상·성별·가격·사이즈·소재는 각각의 전용 인자에 넣습니다.
   - 사용자가 특정 상품명을 직접 말하면 product_name 에 넣습니다.
     상품명 검색을 semantic_query 로 대신하지 마세요.
   - 나머지 용도·기능·착용감만 semantic_query 에 짧은 자연어로 넣습니다.
     반팔·민소매·긴팔·크롭처럼 전용 인자가 없는 외형 조건도 semantic_query 에 남깁니다.
     예: "반팔 티셔츠" -> category=티셔츠, semantic_query="반팔".
     정확한 필터 조건만 있고 의미 요구가 없으면 semantic_query 는 생략합니다.
   - sort=rating은 사용자가 "평점" 또는 "별점"을 직접 말했을 때만 씁니다.
     sort=review도 사용자가 "리뷰"를 직접 말했을 때만 씁니다.
     "편한 순", "가벼운 순", "잘 어울리는 순", "추천순"을 rating으로 바꾸지 마세요.
     이런 속성은 semantic_query에 넣고 sort는 생략합니다.
   - search_product가 성공했지만 shown=0이면 의미 유사도 기준을 통과한 상품이
     없다는 뜻입니다. semantic_query나 사용자가 말한 필터를 임의로 제거해 다시
     검색하지 마세요. 결과가 없다고 알리고, 조건을 완화할지 사용자에게 물으세요.
2. 재고나 세탁 방법을 묻는 질문은 get_info 로 확인한 뒤 답합니다.
3. 여러 상품 중 고를 때는 comparing_info 로 비교 정보를 받고,
   어떤 것이 사용자 조건에 맞는지는 당신이 판단해 이유와 함께 설명합니다.
4. Tool 결과의 status 대로 하세요. 같은 호출을 반복하지 마세요.
   invalid_argument: 인자를 고쳐 다시 부르기 / needs_input: data.choices 를 보여 주고 사용자에게 묻기
   (임의로 고르지 않기) / no_match: 없다고 알리고 조건을 멋대로 빼지 않기 / blocked: 이유와 대안 전하기
   / internal_error: 다시 부르지 말고 처리하지 못했다고 알리기.
5. 상품 ID(AF-B07WF7H5W4 같은 값)를 절대 추측하지 마세요. 이것이 가장 흔한 실수입니다.
   - ID 를 모르면 product_name 에 상품명을 넣으세요.
     get_info, add_to_cart, remove_from_cart 는 이름을 받습니다.
     comparing_info 만 product_ids 가 필요하니, 그때는 search_product 로 먼저 찾으세요.
   - 대화에 나온 적 없는 ID 를 만들어내면 엉뚱한 상품이 처리됩니다.
6. Tool 이 돌려준 메시지의 구체적인 값(상품명, 사이즈, 수량)을 기억하세요.
   사용자가 "방금 뺀 것 다시 담아줘" 라고 하면 그 메시지를 보고 그대로 담으면 됩니다.
   이미 알 수 있는 것을 사용자에게 다시 묻지 마세요.
   Tool 결과 원문은 다음 턴에 남지 않고 당신이 쓴 답변만 남습니다.
   그러므로 답변에 상품명·사이즈·수량을 구체적으로 적어 두세요.
   "1개를 뺐습니다" 가 아니라 "메쉬 배색 하이웨이스트 레깅스 M 사이즈 1개를 뺐습니다" 로 씁니다.
   단, 장바구니나 주문의 "현재 내용" 은 예외입니다. 사용자가 화면에서 직접
   담거나 뺄 수 있으므로 이전 대화의 목록은 이미 낡았을 수 있습니다.
   대신 매 요청 앞에 붙는 [현재 상태] 를 쓰세요. 그것이 지금 값입니다.
   장바구니는 거기 전부 실려 있으니 view_cart 를 또 부르지 마세요.
   주문은 최근 몇 건만 실려 있으므로, 주문을 세거나 목록으로 답할 때는
   반드시 search_order 로 조회하세요. 상태 블록에 보이는 것만 보고 답하면
   나머지를 빠뜨립니다.
7. 되돌릴 수 없는 작업(장바구니 삭제, 결제, 주문 취소, 반품 신청)의 확인 절차.

   **당신의 역할은 미리보기를 만드는 것까지입니다. 실행은 앱이 합니다.**
   대상이 정해졌으면 곧바로 Tool 을 호출하세요. "...할까요?" 라는 확인 문장이 돌아오면
   그대로 전하세요. 앱이 승인 버튼을 띄우고, 사용자가 누르면 앱이 실행합니다.
   - 확인 질문을 당신이 먼저 만들지 마세요. 먼저 묻고 답을 받은 뒤 Tool 을 부르면
     사용자가 같은 답을 두 번 하게 됩니다.
   - cancel_order / return_order 는 가능 여부를 스스로 확인하고, 불가능하면 이유와 대안을
     돌려줍니다. *_possible 은 "취소되나요?" 처럼 실행 없이 물어볼 때만 쓰세요.
   - 확인 문장을 전한 뒤 사용자가 말로 "네" 라고 해도 같은 Tool 을 다시 부르지 말고
     버튼을 눌러 달라고 안내하세요. 승인은 말이 아니라 버튼으로만 받습니다(앱이 강제).
     대상이나 수량이 바뀌면 새 확인 문장이 다시 나옵니다.

   단, **대상이 아직 정해지지 않았으면 먼저 물어야 합니다.**
   "취소해줘" 인데 후보 주문이 여러 건이면 어느 것인지 확인하세요.
   이건 실행 동의를 구하는 것이 아니라 대상을 좁히는 것이라 별개입니다.
8. "[현재 상태]" 의 "직전 검색 결과" 는 실제 결과의 순서와 ID 입니다.
   사용자가 "두 번째 거", "아까 비교한 것" 이라고 하면 그 목록에서 찾으세요.
   기억에 의존해 순서를 짐작하지 마세요.
9. 주문을 다룰 때 지킬 것.
   - 주문 ID(ORD-1001 같은 값)를 추측하지 마세요. search_order 로 먼저 찾습니다.
   - "어제 주문한" 은 ordered_days_ago=1, "지난주에 받은" 은 delivered_within_days=7 입니다.
     주문일과 수령일은 다릅니다.
   - 찾은 주문이 여러 건이면 임의로 고르지 말고 어느 것인지 사용자에게 물으세요.
   - 취소가 불가능하면 이유와 함께 Tool 이 알려준 대안을 그대로 안내하세요.
   - 반품은 "접수" 까지입니다. "환불되었습니다" 가 아니라
     "반품이 접수되었고 회수 후 환불됩니다" 라고 알려주세요.
10. 장바구니를 바꾼 뒤(담기·빼기)에는 답변 끝에 현재 장바구니를 알려주세요.
   Tool 이 돌려준 "현재 장바구니: ..." 문장에 이미 들어 있으니 그대로 옮기면 됩니다.
   무엇이 담겨 있는지, 총 몇 개이고 합계가 얼마인지까지 적습니다.
11. 담을 상품을 사용자가 고르지 않았으면("레깅스 하나 담고 싶은데", "니트 하나 찾아서 담아줘")
   검색만 하고 담지 마세요. 앱이 상위 후보를 채팅에 번호로 보여 주니, 어느 것으로 담을지 짧게 물으세요.
   고르는 기준을 말했거나("1등", "가장 싼 거", "평점 제일 높은 거") 고르는 일을 맡겼으면
   ("골라서 담아줘", "알아서 하나씩") 되묻지 말고 요청의 남은 단계까지 진행하세요.
   "1등", "가장 싼 거"처럼 사용자가 상품을 지목했는데 그 상품에 요청 조건(사이즈 등)이 없으면
   다른 상품으로 바꾸지 말고 물으세요.
   사용자가 말한 사이즈가 상품 표기와 다르면(66인데 S/M/L, 허리 24~32) 가까운 값으로 바꾸지 말고
   재고 있는 사이즈를 알려 주며 물으세요.

답변은 간결한 한국어로 합니다. Tool 이름이나 상품 ID 같은 내부 값은
사용자에게 그대로 노출하지 말고 상품명으로 바꿔서 말하세요.

검색 인자 최종 확인: 사용자가 운동화·셔츠·코트처럼 category 목록의 품목을
직접 말했는데 search_product의 category를 빼면 안 됩니다. semantic_query는
용도·기능·착용감만 담당하며 category를 대신하지 않습니다.

사진 검색 인자 최종 확인:
- 사진 검색 툴은 search_by_image_and_text 하나입니다. 조건이 없는 "이거랑 비슷한 거"도
  이 툴이며, [검증된 첨부 이미지]의 query_image_id와 analysis_id를 그대로 넘기세요.
  "이 사진에 뭐가 있어?"처럼 사진 내용을 물으면 [검증된 첨부 이미지]의 "사진 이해"로 답하세요.
- 새 사진이 올라오면 새 검색입니다. 그 전 사진·대화에서 말한 성별·색·가격은 다시 쓰지 말고,
  이번 사진 이후에 사용자가 말한 조건만 인자에 넣으세요. 모르는 성별은 앱이 다시 묻습니다.
- 사용자의 시각 검색 의도를 영어 문장 하나로 충실하게 뽑아 retrieval_query_en에 넣으세요.
  "비슷한/유사한/닮은/더 밝은/더 짧은" 같은 이미지와의 관계를 제거하거나 별도 인자로
  쪼개지 마세요. 사용자가 말하지 않은 색·스타일·품목은 추가하지 마세요.
- [검증된 첨부 이미지]의 "사진 이해" 값(종류·색·소재)은 VLM 추정값입니다. 사용자가
  직접 말하지 않았다면 category/color/material 인자에 옮기지 마세요. 사용자가 "비슷한 재질",
  "이 색으로", "종류 상관없이"라고 말한 의도는 쪼개지 말고 retrieval_query_en 안에 보존하세요.
- 사용자가 직접 말한 가격·재고·사이즈·브랜드·성별·카테고리·색·소재만 구조화 인자에 넣으세요.
  category/color/material을 넣으면 근거가 된 사용자 표현을 user_quotes에 그대로 옮기세요.
  예: "블랙으로" → color="검은색", user_quotes={"color": "블랙"}. 서버가 이 표현을 확인합니다.
  "검은색 말고"처럼 부정한 값은 넣지 마세요. 핏·기장·패턴·스타일은 retrieval_query_en에 둡니다.
- 사진의 search_features_en은 선택한 아이템에서 보이는 무늬·기장·핏·구조의 영어 설명과 근거입니다.
  reference_attributes에 그대로 유지할 축(pattern/length/silhouette/details)을 매번 넣으세요.
  사용자가 바꾸거나 제외하거나 상관없다고 한 축은 뺍니다. '무지로 더 짧게'는 pattern·length를
  빼고, 바꿀 무늬·기장은 retrieval_query_en에 적습니다. '핏만 비슷하게'는 silhouette만 넣습니다.
  단순 '비슷한 것'은 선택한 아이템의 확인 가능한 축을 유지합니다. 전부 무관하면 []입니다.
  서버가 저장된 영어 특징을 검색문에 붙이므로 추정 소재·색상이나 옆 아이템의 특징을 새로
  만들거나 검색문에 반복하지 마세요. 이 값들은 SQL 강제 조건이 아닙니다.
- 사진이 여러 장이면 [현재 상태]의 사진 목록에서 ID를 고르세요. 사용자가 어느 사진인지 말하지 않으면
  방금 올린 사진(1번)입니다. 목록에 없는 사진은 만료됐으니 다시 올려 달라고 안내하세요.
- [검증된 첨부 이미지]에 아이템이 여러 개여도 사용자에게 먼저 묻지 마세요. 먼저
  search_by_image_and_text를 부르면 서버가 아이템 크기를 보고 큰 아이템으로 진행하거나,
  크기가 비슷하면 requires_item_selection으로 선택을 요청합니다. 성별처럼 빠진 조건도 툴이 묻습니다.
  사용자가 아이템이나 품목을 이미 말했으면(“모자랑 비슷한 거”, “2번”) 그 item_id나 category를 넘기세요.
- 결과에 requires_item_selection=true가 있으면 같은 사진을 다시 분석하지 마세요.
  후보를 사용자에게 물은 뒤, 같은 analysis_id에 사용자가 고른 item_id를 넣어
  search_by_image_and_text로 원래 검색을 이어가세요. 크롭은 서버가 합니다.
- 결과 메시지의 "사진의 … 기준으로 찾았습니다", "…도 있습니다", "기준은 풀었습니다"는
  사용자에게 한 줄로 그대로 전하세요. 사용자가 다른 아이템을 원하면 한 마디로 고칠 수 있어야 합니다.
- 폴백 결과의 relative_applied=false이면 "더 밝은" 같은 상대 조건은 순위에 반영되지 않았습니다.
  반영했다고 말하지 말고, comparing_info로 하나씩 확인하지도 말고, 구체 조건을 제안하세요
  (예: "하늘색으로 다시 찾아볼까요?").

search_product가 성공하면 상품 목록은 화면의 검색 결과 그리드에 따로 표시됩니다.
따라서 채팅 답변에서 상품을 하나씩 다시 나열하거나 임의로 추천 이유를 만들지 말고,
조건에 맞는 검색 결과를 표시했다는 사실과 결과 개수만 간단히 안내하세요.
사용자가 비교를 명시적으로 요청한 경우에만 comparing_info로 비교하세요.
"제일 싼", "리뷰 제일 많은", "평점 제일 높은" 상품은 검색 결과의 quick_picks(결과 전체 기준)에서
고르세요. 사이즈 조건이 있으면 available_sizes 에 그 사이즈가 있는 것 중 첫 번째입니다.
확인 버튼은 확인이 필요한 툴(buy_from_cart 등)을 불러야만 앱이 만듭니다. 툴을 부르지 않고
"승인 버튼을 눌러 주세요"라고 쓰지 마세요.
"""


class ToolCall:
    """모델이 요청한 Tool 호출 하나.

    parse_tool_calls 가 모델 응답을 이 모양으로 바꿔 두고, 루프는 이것만 다룬다.

    id        : 결과를 돌려줄 때 짝을 맞추는 식별자
    name      : 부를 Tool 이름 (예: "search_product")
    arguments : 인자 dict (예: {"color": "검은색", "max_price": 150000})
    error     : 인자를 읽지 못한 경우의 사유. 있으면 실행하지 않고 모델에게 돌려준다
    """

    def __init__(self, call_id, name, arguments, error=None):
        self.id = call_id
        self.name = name
        self.arguments = arguments
        # 인자 JSON 이 깨져 있으면 여기에 사유가 담긴다. 이 호출은 실행하지 않는다.
        self.error = error

    def __repr__(self):
        return f"ToolCall({self.name}, {self.arguments})"


# ======================================================================
# 1단계 - 모델 호출
# ======================================================================

def call_model(messages, tools=None):
    """로컬 모델 서버에 요청하고 assistant 메시지를 그대로 반환한다.

    반환 형태:
        {"role": "assistant", "content": "...", "tool_calls": [...]}

    tools 를 함께 보내면 모델이 그 목록 중에서 골라 호출을 요청할 수 있다.
    보내지 않으면 그냥 대화만 한다.
    """
    headers = {"Content-Type": "application/json"}
    if config.LOCAL_API_KEY:
        headers["Authorization"] = f"Bearer {config.LOCAL_API_KEY}"

    payload = {
        "model": config.MODEL_NAME,
        "messages": messages,
        "temperature": config.TEMPERATURE,
        "max_tokens": config.MAX_TOKENS,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        # 툴을 하나만 줬다면 그 툴을 꼭 부르라는 뜻이다(검증기 verify_outcome, 사진 검색 강제).
        payload["tool_choice"] = "required" if len(tools) == 1 else "auto"

    # 서버가 요구하는 표준 외 파라미터를 합칩니다 (.env 의 EXTRA_BODY).
    payload.update(config.EXTRA_BODY)

    response = requests.post(
        f"{config.LOCAL_API_BASE_URL.rstrip('/')}/chat/completions",
        headers=headers,
        json=payload,
        timeout=config.REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    # 형태가 어긋난 응답(choices 빈 목록, message 가 null)은 여기서 ValueError 로 통일한다.
    # 그대로 두면 IndexError·AttributeError 가 run() 밖으로 새어 세션 상태 저장이 건너뛰어진다.
    choices = data.get("choices") if isinstance(data, dict) else None
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError("모델 응답에 assistant 메시지가 없습니다.")
    return message


# ======================================================================
# 2단계 - 모델 응답에서 Tool 호출 뽑아내기
#
# OpenAI 표준 tool_calls 필드만 읽는다. 루프는 ToolCall 만 다룬다.
# ======================================================================

def parse_tool_calls(message):
    """모델 응답의 OpenAI 표준 tool_calls 필드에서 Tool 호출을 뽑는다. 없으면 빈 리스트.

    본문 텍스트는 뒤지지 않는다. 모델이 "이렇게 부르면 됩니다" 라고 설명으로 적은
    JSON 까지 실행되기 때문이다.
    """
    calls = []

    for index, item in enumerate(message.get("tool_calls") or []):
        function = item.get("function", {})
        name = function.get("name", "")
        arguments = function.get("arguments")

        # arguments 는 dict 가 아니라 JSON "문자열" 로 온다. 반드시 풀어야 한다.
        #
        # 깨진 JSON 을 빈 dict 로 바꾸면 안 된다. 인자가 사라진 채로 실행되기 때문이다.
        # search_product 라면 조건 없는 전체 검색이 되고, 수량이 빠진 채 담길 수도 있다.
        # 실행하지 않고 "이래서 못 읽었다" 를 모델에게 돌려주면 스스로 고쳐 보낸다.
        error = None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as problem:
                error = (f"인자 JSON 을 읽지 못했습니다: {problem.msg}. "
                         f"받은 값: {arguments[:120]}")
                arguments = {}

        if error is None and not isinstance(arguments, dict):
            error = f"인자는 객체여야 합니다. 받은 값: {arguments!r}"
            arguments = {}

        calls.append(ToolCall(
            item.get("id") or f"call_{index}",
            name,
            arguments,
            error=error,
        ))
    return calls


# ======================================================================
# 3단계 - 실행 결과를 대화에 덧붙일 메시지로 만들기
# ======================================================================

# 모델 문맥에서만 뺄 결과 필드. 화면·로그용 값(추적 ID, 추천 분기 점수)이거나
# 다른 필드와 같은 값(displayed == shown)이라 토큰만 쓴다. 트레이스 원본에는 남는다.
_MODEL_HIDDEN_KEYS = {
    "search_product": ("displayed",),
}


# 사진 검색 결과 중 모델에게 보내는 필드. 나머지(query_plan, filter_reasons,
# 순위별 점수 …)는 화면·진단용이라 트레이스에만 남는다. 폴백(RRF) 경로는 최상위
# 필드가 24개였고 Qwen 경로와 이름도 달랐다 — 모델은 두 경로를 같은 모양으로 받는다.
_IMAGE_MODEL_KEYS = (
    "shown", "hard_filters", "unapplied_soft_filters", "negated_filters",
    "reference_filters", "reference_filters_relaxed", "relative_applied",
    "visual_summary", "other_items", "image_analysis_id", "quick_picks",
    # 사진에 아이템이 여럿이라 고르게 할 때(다음 검색의 analysis_id/item_id 에 필요)
    "requires_item_selection", "analysis_id", "source_query_image_id", "items",
    "code", "field", "status", "choices", "missing_fields", "known_filters",
)
_IMAGE_PRODUCT_KEYS = ("product_id", "name", "category", "brand", "price", "rating")


def _image_search_view(data):
    view = {key: data[key] for key in _IMAGE_MODEL_KEYS if key in data}
    if "demoted_to_reference" in data and "unapplied_soft_filters" not in view:
        view["unapplied_soft_filters"] = data["demoted_to_reference"]   # 폴백 경로의 옛 이름
    if isinstance(data.get("products"), list):
        view["products"] = [{key: row.get(key) for key in _IMAGE_PRODUCT_KEYS}
                            for row in data["products"] if isinstance(row, dict)]
    return view


def make_tool_result_message(tool_call, result):
    """Tool 실행 결과를 모델에게 돌려줄 메시지로 만든다.

    ensure_ascii=False 가 중요하다. 빼면 한글이 \\uXXXX 로 나가서
    토큰을 몇 배로 잡아먹는다.
    """
    model_result = result
    # 검색 화면에는 최대 50개를 보내지만, 같은 50개 상세 요약을 모델 문맥에
    # 다시 넣을 필요는 없다. 트레이스의 원본 결과는 그대로 두고 모델에게만
    # 상위 일부만 알려준다. 화면은 server.py가 트레이스에서 전체를 꺼낸다.
    if tool_call.name == "search_product" and result.get("success"):
        data = result.get("data") or {}
        products = data.get("products") if isinstance(data, dict) else None
        if isinstance(products, list) and len(products) > 10:
            model_result = dict(result)
            model_result["data"] = {
                **data,
                "products": products[:10],
                "products_sent_to_model": 10,
                "displayed_in_search_grid": len(products),
            }
    if tool_call.name == "search_by_image_and_text" and isinstance(result.get("data"), dict):
        model_result = {**result, "data": _image_search_view(result["data"])}
    hidden = _MODEL_HIDDEN_KEYS.get(tool_call.name)
    data = model_result.get("data") if isinstance(model_result, dict) else None
    if hidden and isinstance(data, dict) and any(key in data for key in hidden):
        model_result = {**model_result,
                        "data": {k: v for k, v in data.items() if k not in hidden}}
    content = json.dumps(model_result, ensure_ascii=False, default=str)

    # tool_call_id 로 "어느 요청에 대한 답인지" 짝을 맞춰준다.
    return {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "name": tool_call.name,
        "content": content,
    }


# 사진 Tool. 사진이 없는 턴에는 모델에게 보이지 않는다(목록의 약 25%). 사진을 받은 뒤
# IMAGE_TOOL_TURNS 턴 동안은 남겨 둔다 — "아까 사진이랑 비슷한데 흰색으로" 같은 후속 요청.
IMAGE_TOOLS = {"search_by_image_and_text"}
# 사진 툴은 이 대화에 아직 만료되지 않은 사진이 있으면 계속 보여 준다(예전: 사진 받은 뒤 3턴).
# 3턴 규칙에서는 "아이템 선택 → 성별 → 결과" 로 세 턴을 쓰고 나면 같은 사진의 다른 아이템("2", "팬츠도")을
# 사진으로 못 찾고 글 검색으로 대신했다(2026-10-06, 정장 바지 → 반바지·레깅스). 상태 블록에 사진 목록을 실어
# 대화 기록이 밀려도 모델이 ID 를 안다. 사진 ID 의 수명(IMAGE_QUERY_TTL_SECONDS)보다 조금 일찍 뺀다.
MAX_PHOTOS_IN_STATE = 3
_PHOTO_ID = re.compile(r"query_image_id=([0-9a-f-]{36})")
_ANALYSIS_ID = re.compile(r"analysis_id=([0-9a-f-]{36})")
_PHOTO_ITEM = re.compile(r"item_\d+=([^\s·;\"]+)")
_AFFIRMATIVE = re.compile(r"\s*(네|넵|예|응|웅|ㅇㅇ|ㅇ|그래|그래요|좋아|좋아요|맞아|맞아요|그걸로|그렇게)\s*[.!~]*\s*")


# 서버가 첨부 사진의 분석 결과를 사용자 메시지 뒤에 붙이는 블록의 머리.
_PHOTO_NOTE = "\n\n[검증된 첨부 이미지]"


def _photo_record(user_message):
    """[검증된 첨부 이미지] 블록에서 사진 ID·분석 ID·아이템 종류를 뽑는다. 블록이 없으면 None."""
    if _PHOTO_NOTE not in user_message:
        return None
    block = user_message.split(_PHOTO_NOTE, 1)[1]
    photo = _PHOTO_ID.search(block)
    if not photo:
        return None
    analysis = _ANALYSIS_ID.search(block)
    return {"query_image_id": photo.group(1),
            "analysis_id": analysis.group(1) if analysis else None,
            "kinds": list(dict.fromkeys(_PHOTO_ITEM.findall(block)))[:4],
            "expires_at": time.time() + config.IMAGE_QUERY_TTL_SECONDS - 60}


# 아이템 번호만 적은 답: "1", "2번", "1번이요", "3번으로"
_ITEM_NUMBER = re.compile(r"\s*(\d{1,2})\s*(?:번째|번)?\s*(?:이요|요|으로|로)?\s*[.!]?\s*")


def _photo_answers(current, history):
    """사진을 올린 턴부터 지금까지 (사용자 말, 바로 앞 모델 답) 목록. 최근 것이 먼저다.

    filter_resolution.user_turns 와 같이 앱 메시지는 건너뛰고 사진 턴에서 멈춘다.
    """
    messages = [message if isinstance(message, dict) else {}
                for message in [*(history or []), {"role": "user", "content": current}]]
    answers = []
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content")
        if messages[index].get("role") != "user" or not isinstance(content, str):
            continue
        if not content.startswith("(앱) "):
            before = messages[index - 1] if index else {}
            asked = before.get("content") if before.get("role") == "assistant" else ""
            answers.append((content.split(_PHOTO_NOTE, 1)[0].strip(),
                            asked if isinstance(asked, str) else ""))
        if _PHOTO_NOTE in content:
            break
    return answers


def _answered_item(items, answers):
    """사용자 답이 가리키는 사진 아이템 하나. 확실하지 않으면 None.

    "모자"처럼 품목을 말했으면 그 품목인 아이템이 하나일 때. "1"처럼 번호만 말했으면 바로 앞
    모델 질문의 "1. 검은색 볼캡 …" 줄에서 품목을 읽고, 품목이 없으면 목록 길이가 후보 수와 같을
    때만 순서대로 맞춘다(모델은 검증 블록의 아이템 순서대로 나열한다).
    """
    def by_category(text):
        found = categories_in_text(text)
        return [item for item in items if item.get("category") in found], found

    for text, asked in answers:
        number = _ITEM_NUMBER.fullmatch(text)
        if number:
            index = int(number.group(1))
            line = re.search(rf"^\s*{index}\s*[.)]\s*(.+)$", asked, re.M)
            if not line:
                return None
            matched, _ = by_category(line.group(1))
            if len(matched) == 1:
                return matched[0]
            listed = len(re.findall(r"^\s*\d{1,2}\s*[.)]\s", asked, re.M))
            return items[index - 1] if listed == len(items) and 1 <= index <= listed else None
        matched, found = by_category(text)
        if len(matched) == 1:
            return matched[0]
        if found:
            return None    # 품목을 말했는데 후보와 하나로 맞지 않는다. 더 앞 말로 넘겨짚지 않는다
    return None


def _has_connective(text):
    """"~하고", "~빼고", "~담고," 처럼 '고' 로 이어지는 말, 그리고 접속어."""
    return bool(re.search(r"[가-힣]고[\s,]", text or "")) or any(
        word in (text or "") for word in
        ("그리고", "그 다음", "그다음", "다음에", "대신", "그리구",
         "이어서", "후에", "뒤에", "도 ", "까지", "랑 "))


# 한 단계 요청에서 검증 없이 끝내도 되는 조회. 사용자 조건을 인자로 옮기는 Tool(검색·추천·담기)은
# 빠지지 않는다 — "봄에 입을 재킷 찾아줘" 에서 모델이 "봄" 을 빠뜨린 것을 검증기가 잡는다.
PLAIN_LOOKUPS = {"view_cart", "get_info", "get_order",
                 "cancel_possible", "return_possible", "comparing_info"}


def _needs_verification(text, external):
    """이 턴의 최종 답을 검증기에 보낼까. 여러 단계 요청이거나 조건을 옮기는 Tool 을 썼으면 보낸다."""
    return _multi_part(text) or any(entry.get("tool") not in PLAIN_LOOKUPS for entry in external)


def _multi_part(text):
    """부탁이 여러 단계로 이어진 요청인가. "찾아서 담아줘"(~서), "넘으면 빼줘"(~면),
    "취소하고"(~고)처럼 단계가 이어지는 말을 본다. 한 단계 조회("ORD-1001 상태 알려줘")는
    모델이 빠뜨릴 것이 없어 검증 호출이 비용만 늘렸다.
    """
    return _has_connective(text) or bool(re.search(r"[가-힣](서|면|며)[\s,]", text or ""))


def _sizes_mentioned(text):
    """사용자 문장에 나온 사이즈 표기들(검색·담기와 같은 표기로)."""
    found = set()
    for token in re.findall(r"[0-9A-Za-z_]+|프리사이즈|프리", text or ""):
        try:
            found.add(normalize_size(token))
        except ValueError:
            pass
    return found


# 검색만 한 턴에 채팅에 함께 적는 후보 수. 번호는 검색 결과 화면의 순서와 같다
# ("두 번째 거 담아줘" 가 last_results 의 두 번째를 가리킨다).
CHAT_PICKS = 10


def _pick_line(product):
    bits = [product.get("name") or product.get("product_id")]
    if product.get("brand"):
        bits.append(product["brand"])
    if product.get("price") is not None:
        bits.append(f"{product['price']:,}원")
    colors = product.get("colors") or []
    if colors:
        bits.append("·".join(map(str, colors[:3])))
    sizes = [str(s) for s in product.get("available_sizes") or []]
    if sizes:
        more = "…" if len(sizes) > 6 else ""
        bits.append("사이즈 " + "/".join(sizes[:6]) + more)
    return " · ".join(bits)


def _last_question(text):
    """모델 답변에서 마지막 질문 문장. 앱이 목록을 새로 쓰더라도 되묻기는 살린다."""
    questions = re.findall(r"[^.!?\n]*\?", text or "")
    return questions[-1].strip() if questions else ""


# 결과 목록을 돌려주는 검색 Tool. 이것만 부른 턴은 앱이 답을 확정한다(_search_grid_reply).
_GRID_SEARCH_TOOLS = frozenset({"search_product", "search_by_image_and_text"})


def tool_history_messages(trace):
    """이번 턴에 실제로 부른 툴을 다음 턴 대화 기록에 넣을 메시지로 만든다.

    예전에는 history 에 [사용자 말, 최종 답 글]만 남겼다. 그런데 최종 답에는 앱이 만든 문장
    (성별 질문, "상품 50개를 화면에 표시했어요. 1. …" 목록)이 섞여 있어서, 다음 턴의 모델에게는
    "assistant 는 툴 없이 바로 목록을 쓴다"는 예시로 보였다. Qwen 은 두 번째 사진에서 그 모양을
    따라 툴 없이 상품을 지어냈다(같은 사진 1 대화로 짝 비교: 기록 그대로 7/14 · 지어냄 7,
    툴 기록 포함 8/8 · 지어냄 0, 2026-10-02 측정).
    결과 원문 대신 상태·한 줄 메시지·표시 개수만 남겨 토큰은 호출당 수십~백여 개다.
    """
    messages = []
    for index, entry in enumerate(trace or []):
        name = entry.get("tool")
        if not name or entry.get("internal"):    # 검증기·차단 기록은 모델이 부른 업무 툴이 아니다
            continue
        result = entry.get("result") or {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        summary = {"status": result.get("status") or ("completed" if result.get("success") else "failed"),
                   "message": str(result.get("message") or "")[:200]}
        if "shown" in data:
            summary["shown"] = data["shown"]
        call_id = f"hist_{secrets.token_hex(4)}_{index}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(
                entry.get("arguments") or {}, ensure_ascii=False, default=str)}}]})
        messages.append({"role": "tool", "tool_call_id": call_id, "name": name,
                         "content": json.dumps(summary, ensure_ascii=False)})
    return messages


# 검색 툴 없이 쓴 "검색 결과" 답. 앱이 만드는 결과 문장·목록 모양을 모델이 흉내 낸 것이다.
_RESULT_PHRASES = ("화면에 표시", "우선 볼 만한 상품")
_PRICED_LINE = re.compile(r"(?m)^\s*\d+\.\s.*\d원")


def _looks_like_search_result(text):
    return any(phrase in text for phrase in _RESULT_PHRASES) or len(_PRICED_LINE.findall(text)) >= 2


FABRICATED_RESULT_NUDGE = (
    "(앱) 이번 요청에서 검색 툴을 부르지 않았는데 답에 검색 결과(상품 목록·화면 표시)가 들어 있습니다. "
    "상품 목록은 검색 툴 결과로만 보여 줄 수 있습니다. 상품을 찾아야 하면 지금 알맞은 검색 툴을 호출하세요. "
    "찾을 필요가 없으면 상품 목록 없이 답하세요.")
PHOTO_TOOL_FIRST_NUDGE = (
    "(앱) 사진 검색은 직접 묻지 말고 먼저 search_by_image_and_text를 부르세요. 아이템이 여러 개여도 "
    "어느 것으로 찾을지는 툴이 크기를 보고 정하고, 성별처럼 빠진 조건도 툴이 묻습니다. "
    "사용자가 이미 고른 아이템이나 품목이 있으면 item_id나 category로 넘기세요.")
# 사진 내용 자체를 묻는 말("이 사진에 뭐가 있어?")은 검색 요청이 아니다.
_PHOTO_CONTENT_QUESTION = re.compile(r"뭐가|무엇|뭐야|뭔지|무슨|어떤 게 있")
_LIST_LINE = re.compile(r"(?m)^\s*\d{1,2}\s*[.)]\s+\S")


def _asks_photo_choice(text):
    """검색 툴 없이 모델이 직접 쓴 사진 검색 질문(아이템 고르기·성별)인가. 상품 목록(가격)은 아니다."""
    # "원하시는 번호를 알려주세요." 처럼 마침표로 끝나는 질문도 잡는다. 예전에는 놓쳐서 Qwen 이 툴 없이
    # 셔츠/팬츠를 물었는데 툴을 먼저 부르게 하지 못했다(2026-10-07).
    if "?" not in text and not text.rstrip().rstrip(".!~… ").endswith(("까요", "세요", "주세요")):
        return False
    if _PRICED_LINE.search(text):
        return False
    return "남성용·여성용" in text or len(_LIST_LINE.findall(text)) >= 2 or "아이템" in text


FABRICATED_RESULT_ANSWER = "검색을 실행하지 못해 결과를 보여 드리지 못했어요. 같은 요청을 한 번 더 보내 주시겠어요?"


def _search_grid_reply(trace, draft="", user_message="", photo_label="사진"):
    """검색만 수행한 턴의 답은 모델 문장이 아니라 앱이 확정한다.

    모델이 qualified(임계값 통과 전체)를 shown(화면 전달 수)로 잘못 읽어
    화면에는 50개인데 채팅에는 114개라고 말하는 일을 막는다. 개수만 알려 주면 채팅만 보는
    사용자는 무엇이 나왔는지 모르므로 상위 몇 개를 번호와 함께 적고, 모델의 되묻기는 살린다.
    """
    used = [entry for entry in (trace or [])
            if entry.get("tool") and not entry.get("internal")]
    # 사진 검색도 글 검색처럼 앱이 목록을 적는다. 예전에는 글 검색만 여기로 와서, 사진 검색 답은
    # "결과는 화면의 그리드에서 확인하세요" 뿐이라 채팅만 보는 사용자는 무엇이 나왔는지 몰랐다.
    if not used or any(entry.get("tool") not in _GRID_SEARCH_TOOLS for entry in used):
        return None
    for entry in reversed(used):
        result = entry.get("result") or {}
        if not result.get("success"):
            continue
        photo = entry.get("tool") == "search_by_image_and_text"
        data = result.get("data") or {}
        shown = data.get("shown")
        if not isinstance(shown, int):
            continue
        if shown == 0:
            question = _last_question(draft)
            return "조건에 맞는 상품을 찾지 못했습니다." + (f"\n\n{question}" if question else "")
        if photo:
            # photo_label: 사진이 여러 장일 때 "방금 올린 사진" / "앞서 올린 사진" — 어느 사진으로 찾았는지 밝힌다.
            headline = f"{photo_label}과 비슷한 상품 {shown}개를 화면에 표시했어요."
            chosen = (data.get("visual_item") or {}).get("category")
            others = [item.get("category") or "다른 아이템" for item in data.get("other_items") or []]
            if chosen:
                # 무엇을 기준으로 찾았는지 밝힌다. 사용자가 "팬츠도"처럼 고른 경우에도 적는다.
                headline = (f"{photo_label}의 {'소품' if chosen == '기타' else chosen} 기준으로 "
                            f"비슷한 상품 {shown}개를 화면에 표시했어요.")
            if chosen and others:
                # 서버가 큰 아이템으로 자동 진행했을 때 다른 것도 있는지 알린다.
                names = "·".join("소품" if name == "기타" else name for name in dict.fromkeys(others))
                headline += f" 사진에 {names}도 있어요. 그쪽을 찾으시면 말씀해 주세요."
        elif data.get("backfilled"):
            headline = (f"조건에 잘 맞는 상품은 {data.get('qualified', 0)}개이고, "
                        f"비슷한 상품 {data['backfilled']}개도 함께 표시했어요.")
        elif data.get("ranking") == "sql_fallback":
            headline = "의미 검색을 사용할 수 없어 가격·성별·품목 등 필터 조건으로만 찾았어요."
        else:
            headline = f"검색 결과 {shown}개를 화면에 표시했어요."
        parts = [headline]
        top = (data.get("products") or [])[:CHAT_PICKS]
        if top:
            parts.append("우선 볼 만한 상품이에요.\n" + "\n".join(
                f"{i}. {_pick_line(p)}" for i, p in enumerate(top, 1)))
        question = _last_question(draft)
        if not question and top and "담" in (user_message or ""):
            # 담고 싶다고 했는데 상품을 고르지 않았다(규칙 11). 앱이 대신 담지 않고 묻는다.
            question = "어느 상품으로 담을까요? 번호나 상품명으로 알려 주세요."
        if question:
            parts.append(question)
        return "\n\n".join(parts)
    return None


def _chases_other_photo_item(decision, trace, user_text):
    """검증기가 사용자가 말하지 않은 사진 속 다른 아이템을 더 찾으라고(retry) 하거나 물었나(ask_user).

    사진 검색 결과의 "사진에 팬츠도 있습니다"는 안내인데, GLM 검증기는 이를 할 일로 읽고 6번 중
    2번 "팬츠도 추가 검색" retry 를 냈다. 모델이 따라 팬츠를 찾아 셔츠 결과가 사라졌다(2026-10-06).
    """
    if (decision or {}).get("verdict") not in ("retry", "ask_user"):
        return False
    others = set()
    for entry in trace or []:
        data = (entry.get("result") or {}).get("data")
        if entry.get("tool") == "search_by_image_and_text" and isinstance(data, dict):
            others |= {item.get("category") for item in data.get("other_items") or []} - {None}
    if not others:
        return False
    asked = " ".join([*map(str, decision.get("missing_requirements") or []),
                      str(decision.get("next_instruction") or ""), str(decision.get("question") or "")])
    chased = set(categories_in_text(asked)) & others
    return bool(chased) and not (set(categories_in_text(user_text)) & others)


def _picks_line(picks):
    """quick_picks 를 상태 줄로: 최저가·리뷰 많은 상품의 ID·가격·재고 사이즈."""
    if not isinstance(picks, dict):
        return ""
    cheap = ", ".join(f"{row['name']}({row['product_id']}) {row['price']:,}원 [{'/'.join(row.get('available_sizes') or [])}]"
                      for row in (picks.get("cheapest") or [])[:3])
    reviews = ", ".join(f"{row['name']}({row['product_id']}) 리뷰 {row['review_count']:,}"
                        for row in (picks.get("most_reviews") or [])[:2])
    return "; ".join(part for part in (f"최저가: {cheap}" if cheap else "",
                                       f"리뷰 많은 순: {reviews}" if reviews else "") if part)


# 툴을 부르지 않고 확인 버튼이 있는 것처럼 쓴 답. 버튼은 확인이 필요한 툴을 불러야만 생긴다.
_BUTTON_CLAIM = re.compile(r"(승인|확인|결제) ?버튼|버튼(을|으로)? ?(눌러|확인|승인)")
_ACTION_REQUEST = re.compile(r"결제|주문|취소|반품|빼")
BUTTON_CLAIM_NUDGE = (
    "(앱) 확인 버튼은 아직 없습니다. 버튼은 확인이 필요한 툴(buy_from_cart·cancel_order·return_order·"
    "remove_from_cart)을 불러야만 앱이 만듭니다. 사용자가 요청한 작업이 남았으면 지금 그 툴을 부르세요. "
    "남은 작업이 없으면 버튼 이야기 없이 답하세요.")
EMPTY_ANSWER_NUDGE = ("(앱) 답이 비어 있습니다. 필요한 툴을 부르거나, 사용자에게 보낼 답을 "
                      "본문으로 짧게 쓰세요.")
BUTTON_CLAIM_NOTE = "확인 버튼은 아직 만들어지지 않았어요. 진행하시려면 다시 한 번 말씀해 주세요."
# 사진 내용만 묻는 말("이 사진에 뭐가 있어?")에는 검색 의도가 없다.
_SEARCH_INTENT = re.compile(r"찾|비슷|추천|보여|담|구매|검색|사고 싶|살래|팔아|파는")
PHOTO_CONTENT_NOTE = ("사용자는 사진에 무엇이 있는지만 물었습니다. 검색하지 말고 [검증된 첨부 이미지]의 "
                      "사진 이해 내용으로 무엇이 보이는지 답하세요. 비슷한 상품을 찾아 줄지 물어도 됩니다.")
COMPARE_FIRST_NOTE = ("사용자가 비교를 요청했습니다. 담기 전에 comparing_info 로 후보를 비교하고, "
                      "비교 결과에서 사용자 기준(리뷰·평점·가격)에 맞는 상품을 고르세요.")


def _offered_other_items(trace):
    """이번 턴 사진 검색 답이 사진 속 다른 아이템을 이미 안내했나."""
    return any(entry.get("tool") == "search_by_image_and_text"
               and (((entry.get("result") or {}).get("data") or {}).get("other_items"))
               for entry in trace or [] if isinstance((entry.get("result") or {}).get("data"), dict))


def _assistant_message(message, calls):
    """모델이 무엇을 요청했는지를 대화에 남기기 위한 메시지.

    이걸 빼먹으면 다음 턴에서 모델이 자기가 뭘 요청했는지 모른다.
    또 OpenAI 형식은 tool 결과 앞에 반드시 tool_calls 를 가진 assistant 메시지가
    있어야 하므로, 없으면 서버가 400 을 돌려준다.
    """
    if calls:
        return {
            "role": "assistant",
            # content 가 None 이면 거부하는 서버가 있어 빈 문자열로 바꾼다
            "content": message.get("content") or "",
            # function.arguments 는 서버가 준 JSON "문자열" 그대로 되돌린다.
            # dict 로 풀어 보내면 지금 서버는 400 을 돌려준다 (2026-09-11 확인).
            "tool_calls": message.get("tool_calls", []),
        }

    return {"role": "assistant", "content": message.get("content") or ""}


# ======================================================================
# 4단계 - 에이전트 루프
# ======================================================================

# ======================================================================
# 사용자 승인이 필요한 작업
#
# 모델이 confirm=True 를 보내도 실행하지 않는다("미리보기만 보여줘" 에도 confirm=True 를 보내
# 장바구니가 비워진 적이 있다). 루프는 미리보기만 돌려 확인 대기(PendingAction)를 열고,
# 실행은 사용자가 화면 버튼을 눌러 approve() 가 불릴 때만 한다. 승인은 버튼으로만 받는다.
# ======================================================================

CONFIRM_REQUIRED = {"remove_from_cart", "cancel_order", "return_order", "buy_from_cart"}

# 상태를 바꾸지 않는 조회 Tool. 확인 대기가 열릴 때 이 결과는 먼저 답으로 내보낸다.
INFORMATIONAL_TOOLS = {"get_info", "comparing_info", "get_order",
                       "cancel_possible", "return_possible", "view_cart",
                       "search_order", "search_product",
                       "search_by_image_and_text"}

# 실행되면 되돌릴 수 없는(상태를 바꾸는) Tool.
# 도중에 오류가 나도 이것들이 성공했으면 사용자에게 알려야 한다.
STATE_CHANGING = {"add_to_cart", "remove_from_cart",
                  "cancel_order", "return_order", "buy_from_cart"}


# 거절 안내에서 "무엇을" 거절했는지 말하기 위한 이름표.
ACTION_NAME = {
    "remove_from_cart": "장바구니 빼기",
    "cancel_order": "주문 취소",
    "return_order": "반품 신청",
    "buy_from_cart": "주문",
}


class PendingAction:
    """사용자 확인을 기다리는 작업들.

    keys 가 하나가 아니라 **목록**인 이유가 있다.

    "두 주문 모두 취소해줘" 처럼 대상이 여럿이면, 확인은 한 번에 받되
    실행은 Tool 이 한 건씩만 할 수 있다. 열쇠를 합집합 하나로 만들면
    모델이 만들 수 있는 한 칸짜리 열쇠와 영영 맞지 않아 교착에 빠진다.

    그래서 목록으로 들고 있다가, 들어오는 호출이 **그중 하나와 맞으면**
    통과시키고 그 항목을 뺀다. 사용자가 승인한 범위 안에서 실제로 요청한
    것만 처리되므로 의미도 맞다.
    """

    def __init__(self, tool, items, summary, turn, created_at=None, from_request=False):
        self.tool = tool
        # 채팅 요청(run)이 연 확인인가. 주문 화면 버튼(_open_pending)으로 연 확인은 False 다.
        # 승인 뒤 이어가기(continue_after_approval)는 이것이 True 일 때만 원래 요청을 잇는다.
        self.from_request = bool(from_request)

        # [{"key": ..., "label": ..., "arguments": {...}}]
        #
        # 인자를 함께 들고 있는 이유가 있다. 화면 버튼으로 승인하면 모델이
        # 다시 호출해 주지 않으므로, 앱이 그 자리에서 실행할 수 있어야 한다.
        # 열쇠만 들고 있던 때는 "승인은 받았는데 무엇을 실행할지 모르는" 상태가 됐다.
        self.items = list(items)

        self.summary = summary   # 사용자에게 보여준 문장
        self.turn = turn         # 이 기록이 만들어진 턴 번호
        # 만들어진 시각. 턴은 사용자가 말을 해야 넘어가는데, 화면 버튼은 말 없이
        # 한참 뒤에 눌릴 수 있다. 그래서 시간으로도 만료시킨다 (PENDING_TTL_SECONDS).
        self.created_at = time.time() if created_at is None else created_at

    def seconds_left(self, now=None):
        """만료까지 남은 초. 0 이하면 만료."""
        now = time.time() if now is None else now
        return config.PENDING_TTL_SECONDS - (now - self.created_at)

    def expired(self, now=None):
        return self.seconds_left(now) <= 0

    def to_dict(self):
        return {"tool": self.tool, "items": self.items, "summary": self.summary,
                "turn": self.turn, "created_at": self.created_at,
                "from_request": self.from_request}

    @classmethod
    def from_dict(cls, data):
        return cls(data["tool"], data.get("items") or [], data.get("summary", ""),
                   data.get("turn", 0), data.get("created_at"),
                   from_request=data.get("from_request", False))

    @property
    def keys(self):
        return [item["key"] for item in self.items]

    def find(self, key):
        for item in self.items:
            if item["key"] == key:
                return item
        return None

    def take(self, key):
        """목록에 있으면 빼고 True. 없으면 False."""
        for index, item in enumerate(self.items):
            if item["key"] == key:
                self.items.pop(index)
                return True
        return False

    def empty(self):
        return not self.items


# 한 번의 확인에 묶을 수 있는 최대 건수.
# 확인 문장이 길어질수록 사용자가 읽지 않고 승인한다.
MAX_CONFIRM_AT_ONCE = 5

# 앞 작업의 확인 대기 때문에 이번 턴에 미룬 호출에 돌려주는 결과.
_POSTPONED_RESULT = {"success": False, "data": None,
                     "message": "앞 작업의 확인 대기로 보류했습니다. 승인 뒤 이어서 처리합니다. "
                                "다시 부르지 마세요."}

# 승인 대기가 유효한 턴 수. 지나면 버리고 처음부터 다시 확인받는다.
PENDING_TTL_TURNS = 2


def _target_ids(rows):
    """미리보기 줄들이 가리키는 대상 식별자 집합."""
    return {(row.get("order_id"), row.get("product_id"), row.get("size"))
            for row in rows}


def _overlaps(rows, preview_result):
    """이미 묶인 대상과 새 미리보기의 대상이 겹치는지."""
    new_rows = ((preview_result or {}).get("data") or {}).get("preview") or []
    return bool(_target_ids(rows) & _target_ids(new_rows))


def _rows_amount(rows):
    """미리보기 줄들의 금액 합계. 금액이 없는 Tool 이면 None.

    열쇠(_key_from_rows)에는 금액이 들어가지 않는다.
    같은 상품 같은 수량인데 가격만 바뀐 경우를 열쇠로는 잡을 수 없어서,
    승인 시점에 본 금액을 따로 들고 비교한다.
    """
    total = 0
    found = False
    for row in rows:
        price = row.get("price")
        if price is None:
            continue
        found = True
        try:
            total += int(price)
        except (TypeError, ValueError):
            return None
    return total if found else None


def _snapshot(preview_result):
    """미리보기 결과에서 "무엇을 얼마에 처리할 것인가" 를 뽑는다.

    승인 버튼을 누르는 시점에 이걸 다시 계산해서 비교한다.
    사용자가 본 화면과 실제로 실행될 내용이 같은지 확인하는 유일한 방법이다.
    """
    rows = ((preview_result or {}).get("data") or {}).get("preview") or []
    return {"key": _key_from_rows(rows),
            "amount": _rows_amount(rows),
            "rows": list(rows)}


def _key_from_rows(rows):
    """미리보기 줄 목록을 "무엇을 얼마나 처리할 것인가" 의 키 문자열로 만든다. 순서는 무시한다.

    인자 문자열을 그대로 쓰면 안 된다. 같은 대상을 한 번은 상품명으로, 한 번은
    product_id 로 가리키면 문자열이 달라 승인이 영영 성립하지 않았다(실제로 재현됐다).
    그래서 미리보기가 계산해 준 실제 대상(order_id, product_id, size, quantity)으로 만든다.
    """
    # order_id 를 함께 넣는다. 주문 취소·반품은 product_id 만으로 구분되지 않는다.
    # (같은 상품을 두 번 주문했으면 두 주문의 키가 같아진다)
    canonical = sorted(
        (row.get("order_id"), row.get("product_id"), row.get("size"), row.get("quantity"))
        for row in rows
    )
    return json.dumps(canonical, ensure_ascii=False, default=str)


class ShoppingAgent:
    """대화 하나를 담당하는 에이전트.

        store = Store(db_path=dsn, user_id=user_id)
        agent = ShoppingAgent(store)
        answer, trace = agent.run("15만원 이하 검은색 운동화 찾아줘")

    store 를 인자로 받는 이유: 사용자마다 Store 가 따로이므로(server.py)
    같은 창고를 agent 와 toolbox 가 함께 바라보게 해야 한다.
    """

    def __init__(self, store):
        self.store = store
        self.toolbox = Toolbox(store)

        # 턴 번호. 사용자 요청 하나가 한 턴이다.
        # 승인이 "이전 턴에 만들어졌는지" 를 보려면 시간 개념이 필요하다.
        self.turn = 0

        # 사용자 확인을 기다리는 작업. 한 번 쓰면 지운다.
        self.pending = None

        # 확인 대기가 하나 잡혀서 미뤄 둔 작업. {"items": [...], "turn": n}
        # 사용자가 한 문장에 두 가지를 부탁했을 때 뒤의 것을 잃지 않기 위한 것이다.
        self.postponed = None

        # 직전 검색·비교 결과. "두 번째 상품" 같은 말을 실제 ID 로 잇기 위한 것.
        # 대화 내역에는 Tool 결과가 남지 않으므로 여기에 따로 들고 있는다.
        # 매번 덮어쓰므로 무한정 쌓이지 않는다.
        self.last_results = None

        # 직전에 조회한 주문(search_order/get_order). "그거 취소해줘" 의 "그거" 가 가리키는 대상.
        # 주문 전체 목록은 상태 블록에 따로 있지만, 방금 이야기한 주문이 어느 것인지는 없었다.
        self.last_orders = None

        # 직전 검색의 인자와 결과 개수. 다음 사용자 턴의 "그중", "더 저렴한"을 잇는다.
        self.last_search = None
        self.search_intake = None

        # 다중 아이템 사진에서 한 번 만든 VLM 분석을 다음 사용자 턴까지 보존한다.
        # 그렇지 않으면 사용자가 대상을 고를 때 같은 사진을 다시 분석하게 된다.
        self.last_image_analysis = None
        # 사진 턴부터 지금까지의 사용자 답. 모델이 빠뜨린 item_id 를 채울 때 읽는다(run 마다 갱신).
        self.photo_answers = []
        # 이 대화에 올라온 사진 목록(오래된 것부터). 상태 블록에 유효한 것만 싣는다.
        self.photos = []
        # 앞 사진 검색에서 쓴 성별. 새 사진에서 "이번에도 남성용으로 찾을까요?" 로 제안할 때 쓴다.
        self.last_photo_gender = None

        # 마지막 실제 사용자 요청. 승인 버튼으로 턴이 끊긴 뒤 "원래 부탁 중 남은 것" 을
        # 이어가기 위해 든다. 앱이 만든 이어가기 문장(RESUME_MESSAGE)은 여기 넣지 않는다.
        self.last_request = None
        # 직전 approve() 결과. continue_after_approval 이 한 번 읽고 지운다(저장하지 않는 값).
        self._approval_outcome = None
        # 승인 뒤 이어가는 run 인가. 그 run 은 Tool 없이 끝나도 검증기에 보낸다(저장하지 않는 값).
        self._resuming = False

        # 사용자가 명시적으로 장기 기억을 요청한 선호만 저장한다.
        self.preferences = agency.PreferenceMemory()

    # ------------------------------------------------------------------
    # 프롬프트 조립
    #
    # 고정된 것과 매 턴 바뀌는 것을 나눠 둡니다.
    # 서버는 요청의 "앞에서부터 같은 부분" 을 재사용합니다 (prefix caching).
    # 그래서 바뀌는 값이 앞에 있으면 그 뒤가 전부 무효가 됩니다.
    #
    #   전:  [system: 규칙 + 현재 상태]  [대화]  [질문]  + tools
    #              └ 매 턴 바뀜 -> 뒤의 tools 2,500 토큰까지 다시 계산
    #
    #   후:  [system: 규칙]  [대화]  [현재 상태 + 질문]  + tools
    #        └───── 안 바뀜, 재사용 ─────┘  └ 여기만 새로
    #
    # 덤으로 모델은 긴 프롬프트의 가운데를 흘리는 경향이 있어서,
    # 최신 정보가 뒤에 있는 편이 더 잘 읽힙니다.
    # ------------------------------------------------------------------

    # 현재 상태에 늘어놓을 최대 줄 수.
    # 전부 나열하면 주문이 쌓일수록 프롬프트가 무한정 커집니다.
    # 관찰은 덤프가 아니라 요약이어야 하고, 자세한 건 Tool 로 가져오면 됩니다.
    MAX_STATE_CART_LINES = 8
    MAX_STATE_ORDERS = 5

    def _cart_line(self):
        cart = self.store.view_cart()
        if cart["count"] == 0:
            return "비어 있음"

        shown = cart["items"][: self.MAX_STATE_CART_LINES]
        items = " / ".join(
            f"{item['name']}({item['product_id']}) {item['size']} 사이즈 {item['quantity']}개"
            for item in shown
        )
        if len(cart["items"]) > len(shown):
            items += f" 외 {len(cart['items']) - len(shown)}종"
        return f"{items} — 총 {cart['quantity']}개, 합계 {cart['total']:,}원"

    def _order_line(self):
        orders = sorted(self.store.orders, key=lambda o: o["ordered_at"], reverse=True)
        if not orders:
            return "없음"

        shown = orders[: self.MAX_STATE_ORDERS]
        line = " / ".join(
            f"{order['order_id']} {order['product_name']}({order['status']})"
            for order in shown
        )
        if len(orders) > len(shown):
            line += f" 외 {len(orders) - len(shown)}건 (search_order 로 조회)"
        return line

    def _photo_label(self, trace):
        """이번 사진 검색이 어느 사진이었나. 유효한 사진이 한 장뿐이면 그냥 "사진"."""
        photos = self._active_photos()
        if len(photos) < 2:
            return "사진"
        for entry in reversed(trace or []):
            data = ((entry.get("result") or {}).get("data") or {}) if entry.get("tool") == "search_by_image_and_text" else {}
            analysis = data.get("image_analysis_id") if isinstance(data, dict) else None
            if analysis:
                return "방금 올린 사진" if analysis == photos[0].get("analysis_id") else "앞서 올린 사진"
        return "사진"

    def _photo_of(self, arguments, data=None):
        """검색 인자(analysis_id·query_image_id)가 가리키는 사진 기록."""
        ids = {(arguments or {}).get("analysis_id"), (arguments or {}).get("query_image_id"),
               (data or {}).get("image_analysis_id") if isinstance(data, dict) else None} - {None, ""}
        for photo in reversed(self.photos):
            if ids & {photo.get("analysis_id"), photo.get("query_image_id")}:
                return photo
        return None

    def _photo_contents_answer(self):
        """사진 내용 질문에 앱이 직접 답한다(모델이 계속 검색하려 할 때)."""
        photos = self._active_photos()
        kinds = "·".join("소품" if kind == "기타" else kind for kind in (photos[0].get("kinds") if photos else []) or [])
        if not kinds:
            return "사진 내용을 확인하지 못했어요. 사진을 다시 올려 주시겠어요?"
        return f"사진에는 {kinds}가 보여요. 이 중 비슷한 상품을 찾아 드릴까요?"

    def _active_photos(self, now=None):
        """아직 만료되지 않은 사진. 최근 것이 먼저다."""
        now = now or time.time()
        return [p for p in reversed(self.photos) if p.get("expires_at", 0) > now]

    def _photos_line(self):
        photos = self._active_photos()
        if not photos:
            return ""
        parts = []
        for index, photo in enumerate(photos, 1):
            kinds = "·".join(photo.get("kinds") or []) or "아이템 정보 없음"
            until = time.strftime("%H:%M", time.localtime(photo["expires_at"]))
            label = "방금 올린 사진" if index == 1 else "앞서 올린 사진"
            ids = f"query_image_id={photo['query_image_id']}"
            if photo.get("analysis_id"):
                ids += f", analysis_id={photo['analysis_id']}"
            parts.append(f"{index}) [{label}] {kinds} ({ids}, {until}까지)")
        return ("- 사진(유효한 것, 최근 것이 먼저): " + " / ".join(parts)
                + ". 사용자가 어느 사진인지 말하지 않으면 1)로 찾으세요. 목록에 없는 사진은 만료됐으니 다시 올려 달라고 하세요.")

    def _state_block(self):
        """이 요청 시점의 장바구니·주문을 글로 적는다.

        모델이 이전 대화의 view_cart 결과를 그대로 재사용하는 문제가 있었다.
        사용자가 화면에서 직접 담거나 빼면 대화 내역은 그 순간 낡는다.
        (관찰: 채팅으로 장바구니 확인 -> 화면에서 클릭으로 담기 ->
               다시 물어보면 옛날 목록을 그대로 답했다)

        "매번 Tool 로 다시 확인하라" 고 지시하는 것보다,
        매 턴 실제 상태를 함께 보내는 쪽이 확실하다.
        모델의 성실함에 기대지 않고 사실을 손에 들려주는 방식이다.
        """
        lines = [
            "[현재 상태] 이 요청이 시작된 시점의 실제 값입니다.",
            f"- 장바구니: {self._cart_line()}",
            f"- 주문: {self._order_line()}",
        ]

        if config.USER_MEMORY_ENABLED:
            lines.append(f"- 사용자 선호 메모리: {self.preferences.state_line()}")
            gender = self.preferences.public().get("gender")
            if gender:
                lines.append(f"- 확인된 기본 상품 성별: {gender[0]}. 이번 요청에 다른 성별이나 선물 대상이 "
                             "없으면 이 값을 검색에 쓰고 성별을 다시 묻지 마세요.")
        if self.search_intake:
            lines.append("[확인 중인 검색] " + json.dumps(self.search_intake, ensure_ascii=False))
        photos_line = self._photos_line()
        if photos_line:
            lines.append(photos_line)

        recent = self._recent_line()
        if recent:
            lines.append(f"- 직전 {recent}")

        search_context = self._search_context_line()
        if search_context:
            lines.append(f"- 직전 검색 조건: {search_context}")

        if self.last_orders:
            # 상태는 지금 값으로 적는다. 조회 뒤에 취소·반품됐으면 저장된 상태는 낡았다.
            current = {order["order_id"]: order["status"]
                       for order in self.store.orders}
            orders = " ".join(
                f"{index}) {row['order_id']} {row['product_name']}"
                + (f" {row['color']}" if row.get("color") else "")
                + f"({current.get(row['order_id'], row['status'])})"
                for index, row in enumerate(self.last_orders, 1))
            lines.append(
                f"- 직전 조회 주문: {orders}\n"
                "  \"그거\", \"그 주문\" 은 이 목록을 가리킵니다. 한 건이면 다시 조회하지 말고 "
                "그 order_id 로 바로 처리하세요.")

        if self.last_image_analysis:
            context = self.last_image_analysis
            candidates = ", ".join(
                f"{item.get('item_id')}={item.get('category') or '종류 미상'}"
                for item in context.get("items") or [])
            lines.append(
                "- 대기 중인 사진 아이템 선택: "
                f"analysis_id={context.get('analysis_id')}, 후보={candidates}."
                " 사진을 다시 분석하지 말고 저장된 analysis_id/item_id를 "
                "재사용하세요. 원래 검색 인자="
                f"{json.dumps(context.get('search_arguments') or {}, ensure_ascii=False)}"
            )

        waiting = self._postponed_line()
        if waiting:
            lines.append(
                f"- 이어서 할 일: 사용자가 같은 요청에서 함께 부탁했는데 아직 안 한 작업입니다 — "
                f"{waiting}\n"
                f"  앞 작업이 끝났으면 이것을 이어서 처리하세요. "
                f"사용자가 다시 말하지 않아도 됩니다."
            )

        if self.pending is not None:
            # 모델은 승인 여부를 판단하지 않는다. 승인은 버튼으로만 받으므로 "동의했으면 다시
            # 호출하라" 대신 "관여하지 말라" 를 알린다. 내용을 바꾸는 요청만 다시 호출하게 한다.
            lines.append(
                f"- 확인 대기: 방금 사용자에게 이렇게 물었습니다 — "
                f"\"{self.pending.summary}\"\n"
                f"  이 작업은 사용자가 **화면의 버튼**을 눌러야 실행됩니다.\n"
                f"  사용자가 말로 동의하기만 했다면 {self.pending.tool} 를 다시 부르지 말고, "
                f"버튼을 눌러 달라고 안내하세요.\n"
                f"  **단, 대상·수량·사유 같은 내용을 바꿔 달라고 하면 반드시 "
                f"{self.pending.tool} 를 바뀐 값으로 다시 호출하세요.** "
                f"그래야 화면의 버튼도 바뀐 내용으로 갱신됩니다.\n"
                f"  호출하지 않고 \"바꿔 드렸습니다\" 라고만 답하면, "
                f"버튼은 이전 내용 그대로라 사용자가 다른 것을 승인하게 됩니다.\n"
                f"  다른 것을 요청했으면 그 요청을 처리하세요. 이 대기는 무시하면 됩니다."
            )

        lines.append(
            "이전 대화에 적힌 목록은 신뢰하지 마세요. 위 [현재 상태] 가 지금 값입니다.\n"
            "  - 장바구니: 위가 **전부**입니다. 내용을 알려고 view_cart 를 또 부르지 마세요.\n"
            "  - 주문: 위는 **최근 몇 건만** 보여 준 것입니다. 주문을 세거나 목록으로 "
            "답하거나 조건으로 찾을 때는 반드시 search_order 로 조회하세요.\n"
            "  - 그 밖의 것(상품 재고, 세탁 방법, 주문 상세)은 해당 Tool 로 확인하세요."
        )
        return "\n".join(lines)

    def _recent_line(self):
        """직전 검색·비교 결과를 번호와 함께 한 줄로 만든다.

        사용자는 "두 번째 거 담아줘", "아까 비교한 것 중에" 처럼 말한다.
        그런데 다음 턴에는 Tool 결과가 대화에 남지 않아서, 모델은 자기가 쓴
        답변 문장만 보고 순서를 짐작해야 했다. 그러면 엉뚱한 상품이 담긴다.
        번호와 실제 ID 를 함께 주면 짐작할 일이 없다.
        """
        recent = self.last_results
        if not recent:
            return ""

        label = {"search_product": "검색 결과", "search_by_image_and_text": "사진 검색 결과"}.get(
            recent["tool"], "비교 결과")
        items = " ".join(
            f"{index}) {item['name']}({item['product_id']})"
            for index, item in enumerate(recent["items"], 1)
        )
        picks = recent.get("picks")
        return f"{label}: {items}" + (f"\n  결과 전체 기준 {picks}. 이 상품들은 다시 검색하지 말고 ID로 쓰세요."
                                      if picks else "")

    def _search_context_line(self):
        """후속 요청에서 유지하거나 바꿀 수 있도록 직전 검색 인자를 보여 준다."""
        if not self.last_search:
            return ""
        arguments = json.dumps(self.last_search.get("arguments") or {},
                               ensure_ascii=False, sort_keys=True, default=str)
        total = self.last_search.get("total")
        shown = self.last_search.get("shown")
        counts = []
        if isinstance(total, int):
            counts.append(f"후보 {total}개")
        if isinstance(shown, int):
            counts.append(f"화면 {shown}개")
        suffix = f" ({', '.join(counts)})" if counts else ""
        return arguments + suffix

    def _user_message(self, user_message, image_data_url=None):
        """현재 상태를 앞에 붙인 사용자 메시지.

        상태를 별도의 system 메시지로 끼워 넣지 않고 사용자 메시지에 붙인 이유:
        대화 중간의 system 메시지를 무시하거나 첫 번째만 인정하는 서버가 있습니다.
        그러면 상태가 조용히 사라져 원래 버그로 되돌아갑니다.
        user 메시지는 어떤 서버든 반드시 읽으므로 이쪽이 안전합니다.

        여기서 만든 문자열은 대화 기록에는 남지 않습니다.
        server.py 는 상태 블록을 붙이기 전의 사용자 메시지만 저장합니다.
        """
        text = f"{self._state_block()}\n\n{user_message}"
        if image_data_url:
            return {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]}
        return {"role": "user", "content": text}

    # ------------------------------------------------------------------
    # 승인 관리
    # ------------------------------------------------------------------

    def _consume_postponed(self, tool_name, arguments):
        """미뤄 둔 작업 중 지금 실행한 것을 지운다.

        Tool 이름만 보고 지우면 안 된다. "주문 두 건 취소" 는 둘 다
        cancel_order 라서, 한 건을 처리하는 순간 나머지 한 건까지 사라진다.
        인자까지 같아야 같은 작업이다.
        """
        if not self.postponed:
            return
        target = json.dumps({k: v for k, v in arguments.items() if k != "confirm"},
                            sort_keys=True, ensure_ascii=False, default=str)

        remaining = []
        removed = False
        for item in self.postponed["items"]:
            same = json.dumps(item["arguments"], sort_keys=True,
                              ensure_ascii=False, default=str)
            if not removed and item["tool"] == tool_name and same == target:
                removed = True          # 같은 것 하나만 지운다
                continue
            remaining.append(item)

        self.postponed = ({"items": remaining, "turn": self.postponed["turn"]}
                          if remaining else None)

    def _postponed_line(self):
        """미뤄 둔 작업을 프롬프트에 실을 문장으로 만든다.

        세 턴이 지나도 처리되지 않았으면 사용자가 마음을 바꾼 것으로 보고 버린다.
        계속 들고 있으면 한참 뒤에 엉뚱하게 되살아난다.
        """
        if not self.postponed:
            return ""
        if self.turn - self.postponed["turn"] > 3:
            self.postponed = None
            return ""
        items = " / ".join(
            f"{item['tool']}({json.dumps(item['arguments'], ensure_ascii=False)})"
            for item in self.postponed["items"]
        )
        return items

    def _executed(self, trace):
        """이번 턴에 실제로 상태를 바꾸려고 **시도한** 기록만 고른다.

        미리보기(requires_confirmation)는 아직 아무것도 바꾸지 않았으므로 뺀다.
        성공한 것만이 아니라 실패한 것도 남긴다. 실패했다는 사실 자체가
        사용자에게 전달되어야 하는 정보이기 때문이다.
        """
        return [
            entry for entry in trace
            if entry["tool"] in STATE_CHANGING
            and not entry.get("internal")          # 중복 차단·검증기 기록은 실행이 아니다
            and not (entry["result"].get("data") or {}).get("requires_confirmation")
        ]

    def _result_report(self, trace, interrupted=False, always=False):
        """실행 결과를 **앱이** 문장으로 만든다.

        최종 답변은 모델이 쓴다. 그런데 세 건 중 두 건만 성공해도
        모델은 "두 건 모두 처리했습니다" 라고 쓸 수 있다.
        데이터는 store 가 실행부에서 다시 판정하므로 안전하지만,
        **보고가 틀린다.** 사용자 입장에서는 둘이 구분되지 않는다.

        판단하는 곳이 한 곳이어야 하듯 보고하는 곳도 한 곳이어야 한다.
        그래서 trace 에서 사실을 뽑아 모델 문장 아래에 붙인다.
        모델이 무엇을 쓰든 이 줄은 바뀌지 않는다.

        늘 붙이면 시끄러우므로, 모델이 요약하다 틀릴 수 있을 때만 붙인다.
          - 실패가 하나라도 섞여 있을 때
          - 상태를 바꾼 작업이 두 건 이상일 때
          - 모델 문장이 없는 경로일 때 (버튼 승인 always, 중간 실패 interrupted)
        """
        done = self._executed(trace)
        if not done:
            return ""

        failed = [entry for entry in done if not entry["result"].get("success")]
        if not (always or interrupted or failed or len(done) > 1):
            return ""

        lines = []
        for entry in done:
            mark = "\u2713" if entry["result"].get("success") else "\u2717"
            text = entry["result"].get("message") or entry["tool"]
            lines.append(f"  {mark} {text}")

        note = ""
        if interrupted and len(failed) < len(done):
            # 모델 호출이 중간에 끊긴 경우. 이미 반영된 것을 알려 주지 않으면
            # 사용자가 같은 요청을 다시 해서 두 번 처리된다.
            note = "\n  (\u2713 표시된 작업은 이미 반영되었습니다. 다시 요청하지 마세요.)"

        return "\n\n\u2500 실행 결과 \u2500\n" + "\n".join(lines) + note

    def _stale_pending_note(self):
        """이번 턴에 Tool 을 부르지 않았는데 확인 대기가 살아 있으면 알린다.

        모델이 "사유를 바꿔서 다시 확인해 드리겠습니다" 라고 **말만 하고**
        Tool 을 다시 부르지 않은 적이 있다. 그러면 화면의 승인 버튼은
        이전 내용 그대로인데 대화에는 바뀐 것처럼 적힌다.
        사용자는 자기가 말한 것과 다른 것을 승인하게 된다.

        모델이 무엇을 쓰든 사실은 앱이 붙인다.
        """
        pending = self.pending
        if pending is None or pending.turn >= self.turn:
            return ""      # 이번 턴에 새로 잡힌 대기라면 방금 그 문장이 맞다

        labels = "\n".join(f"  {index}. {item['label']}"
                            for index, item in enumerate(pending.items, 1))
        return ("\n\n─ 승인 버튼은 아직 아래 내용입니다 ─\n" + labels
                + "\n  바꾸시려면 무엇을 어떻게 바꿀지 말씀해 주세요.")

    def _remember_results(self, name, result, arguments=None):
        """검색·비교 결과의 상품 ID 와 순서를 기록한다.

        다음 턴에는 Tool 결과 원문이 남지 않으므로, "두 번째 상품" 같은 말을
        실제 ID 로 이으려면 여기에 따로 들고 있어야 한다.
        매번 덮어쓰기 때문에 대화가 길어져도 커지지 않는다.
        """
        data = result.get("data") or {}
        if name == "search_by_image_and_text":
            if result.get("success"):
                self.last_image_analysis = None
                # 사진 검색 결과도 "두 번째 거", 승인 뒤 이어가기가 가리킬 수 있게 남긴다. 예전에는 글 검색만
                # 남겨, 이어가기가 다시 검색하다 앞서 찾은 최저가 대신 다른 상품을 샀다(GLM 2/3).
                rows = data.get("products") if isinstance(data, dict) else None
                if rows:
                    self.last_results = {"tool": name,
                                         "items": [{"product_id": row.get("product_id"), "name": row.get("name")}
                                                   for row in rows if isinstance(row, dict)][:10],
                                         "picks": _picks_line(data.get("quick_picks"))}
                if (arguments or {}).get("gender"):
                    self.last_photo_gender = arguments["gender"]
                    photo = self._photo_of(arguments, data)
                    if photo is not None:
                        photo["gender"] = arguments["gender"]
            elif isinstance(data, dict) and data.get("requires_item_selection"):
                self.last_image_analysis = {
                    "analysis_id": data.get("analysis_id"),
                    "items": [
                        {key: item.get(key) for key in ("item_id", "category")}
                        for item in (data.get("items") or []) if isinstance(item, dict)
                    ],
                    "search_arguments": dict(arguments or {}),
                }
            return
        if name in ("search_order", "get_order"):
            if result.get("success") and isinstance(data, dict):
                rows = data.get("orders") if name == "search_order" else [data]
                self.last_orders = [
                    {"order_id": row.get("order_id"), "product_name": row.get("product_name"),
                     "color": row.get("color"), "status": row.get("status")}
                    for row in rows or [] if isinstance(row, dict) and row.get("order_id")
                ][:5] or None
            return
        if name not in ("search_product", "comparing_info"):
            return
        if not result.get("success"):
            return

        rows = data.get("products") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return

        picked = [
            {"product_id": row.get("product_id"), "name": row.get("name")}
            for row in rows if isinstance(row, dict) and row.get("product_id")
        ][:10]
        if picked:
            self.last_results = {"tool": name, "items": picked,
                                 "picks": _picks_line(data.get("quick_picks") if isinstance(data, dict) else None)}
        elif name == "search_product":
            # 빈 검색 뒤 "그중"이 이전 검색의 상품을 가리키면 안 된다.
            self.last_results = None
        if name == "search_product":
            self.last_search = {
                "arguments": dict(arguments or {}),
                "total": data.get("total") if isinstance(data, dict) else None,
                "shown": data.get("shown") if isinstance(data, dict) else len(rows),
            }

    def _verify_outcome(self, user_message, draft, trace, about_to_confirm=None):
        """하나의 검증 단계에서 완료·안전한 재시도·사용자 질문을 결정한다.

        검증기는 Tool을 실행하지 않는다. 실패해도 기존 답변을 그대로 반환하도록
        격리해 두어, 검증 모델 장애가 주문·승인 흐름을 깨지 않게 한다.
        about_to_confirm 이 있으면 확인 버튼을 띄우기 직전 단계의 검증이다.
        """
        try:
            message = call_model(
                agency.verifier_messages(user_message, draft, trace, self._state_block(),
                                         about_to_confirm=about_to_confirm,
                                         recent_turns=self.toolbox.user_texts),
                tools=[agency.VERIFY_TOOL],
            )
            calls = parse_tool_calls(message)
            if len(calls) != 1 or calls[0].name != "verify_outcome" or calls[0].error:
                return None
            decision = agency.validate_verdict(calls[0].arguments)
            if decision is None:
                return None
            trace.append({
                "tool": "verify_outcome",
                "arguments": calls[0].arguments,
                "result": {"success": True, "data": decision,
                           "message": decision["summary"] or decision["verdict"]},
                "internal": True,
            })
            return decision
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _call_signature(name, arguments):
        return json.dumps({"tool": name, "arguments": arguments}, sort_keys=True,
                          ensure_ascii=False, default=str)

    # ------------------------------------------------------------------
    # 실행
    # ------------------------------------------------------------------

    def _preview(self, tool, arguments, trace):
        """미리보기를 돌리고 기록에 남긴다. 상태는 바뀌지 않는다."""
        clean = {key: value for key, value in arguments.items() if key != "confirm"}
        preview = self.toolbox.call(tool, {**clean, "confirm": False})
        trace.append({"tool": tool, "arguments": {**clean, "confirm": False},
                      "result": preview})
        return clean, preview

    def _entry(self, arguments, preview, tool=""):
        """확인 대기 한 줄. 승인 시점에 비교할 값을 함께 들고 있는다."""
        snap = _snapshot(preview)
        return {"key": snap["key"],
                "amount": snap["amount"],
                "label": preview.get("message", "") or tool,
                "arguments": dict(arguments)}, snap

    def _open_pending(self, requests, trace, note=""):
        """[{tool, arguments}] 를 미리보기 돌려 **새 확인 대기**를 연다.

        모델을 거치지 않는다. 두 곳에서 쓴다.
          - 승인 직전에 내용이 달라져 다시 확인받아야 할 때
          - 앞 작업을 승인한 뒤 미뤄 둔 작업을 이어갈 때

        한 번의 확인은 Tool 하나만 담는다. 다른 Tool 은 다시 미뤄 둔다.
        (섞으면 사용자가 무엇을 승인하는지 흐려지고, 앞 작업 반영 전 상태로
         뒤 작업 미리보기가 계산된다)

        반환: (사용자에게 보여줄 문장, 실행할 수 없던 것들의 사유 목록)
        """
        if not requests:
            return "", []

        tool = requests[0]["tool"]
        entries, rows_all, leftover, problems = [], [], [], []

        for request in requests:
            if request["tool"] != tool or len(entries) >= MAX_CONFIRM_AT_ONCE:
                leftover.append(request)
                continue

            arguments, preview = self._preview(request["tool"],
                                               request["arguments"], trace)
            if not preview.get("success"):
                problems.append(preview.get("message") or request["tool"])
                continue

            if entries and _overlaps(rows_all, preview):
                leftover.append(request)
                continue

            entry, snap = self._entry(arguments, preview, request["tool"])
            entries.append(entry)
            rows_all.extend(snap["rows"])

        self.postponed = ({"items": leftover, "turn": self.turn}
                          if leftover else None)

        if not entries:
            return "", problems

        summary = "\n\n".join(entry["label"] for entry in entries)
        if note:
            summary = note + "\n\n" + summary
        summary += ("\n\n아래 버튼으로 선택해 주세요. "
                    "되돌릴 수 없는 작업이라 채팅 답변으로는 진행되지 않습니다.")
        self.pending = PendingAction(tool=tool, items=entries,
                                     summary=summary, turn=self.turn)
        return summary, problems

    def approve(self, keys=None):
        """화면 버튼으로 들어온 승인. **모델을 거치지 않고** 실행한다.

        모델이 관여하지 않는 것이 핵심이다. "사용자가 무엇에 동의했는가" 가
        자연어 해석 결과가 아니라 사용자가 **누른 열쇠**이므로,
        동의 범위가 어긋날 여지가 없다.

        다만 열쇠가 맞다고 그대로 실행하면 안 된다.
        미리보기를 계산한 시점과 버튼을 누르는 시점 사이에 상태가 바뀔 수 있다.
        (129,000원 결제를 확인받은 뒤 사용자가 화면에서 상품을 더 담으면
         그 버튼으로 238,000원이 결제된다. DB 로 치면 TOCTOU 다.)

        그래서 **실행 직전에 미리보기를 다시 계산해 승인 시점과 비교한다.**
        다르면 실행하지 않고 새 미리보기로 다시 확인받는다.
        """
        # 이어가기(continue_after_approval)가 볼 이번 승인의 결과. 실행 없이 끝나는 경로는 전부 "안 됨".
        self._approval_outcome = {"ok": False, "from_request": False}
        pending = self.pending
        if pending is None:
            return "확인 대기 중인 작업이 없습니다.", []
        from_request = pending.from_request

        if pending.expired():
            # 버튼이 화면에 떠 있는 채로 시간이 지났다. 미리보기를 계산한 시점의
            # 장바구니·주문 상태가 지금과 같다는 보장이 없으므로 실행하지 않는다.
            self.pending = None
            self.postponed = None
            return ("확인 시간이 지나 실행하지 않았습니다. "
                    "필요하시면 다시 요청해 주세요."), []

        self.turn += 1
        targets = (list(pending.keys) if keys is None
                   else [key for key in keys if pending.find(key) is not None])
        if not targets:
            return "선택된 작업이 없습니다.", []

        tool = pending.tool
        trace = []
        executed, stale, blocked = [], [], []

        for key in targets:
            item = pending.find(key)
            if item is None:
                continue
            pending.take(key)

            arguments, preview = self._preview(tool, item["arguments"], trace)

            if not preview.get("success"):
                # 배송이 시작됐거나 재고가 빠졌다. 상태를 바꾸지 않고 사유만 알린다.
                blocked.append(preview.get("message") or f"{tool} 을 실행할 수 없습니다.")
                continue

            snap = _snapshot(preview)
            if snap["key"] != item["key"] or snap["amount"] != item.get("amount"):
                # 승인 화면에 보여 준 내용과 지금 실행될 내용이 다르다.
                stale.append({"tool": tool, "arguments": arguments})
                continue

            result = self.toolbox.call(tool, {**arguments, "confirm": True})
            trace.append({"tool": tool, "arguments": {**arguments, "confirm": True},
                          "result": result})
            self._remember_results(tool, result, {**arguments, "confirm": True})
            self._consume_postponed(tool, arguments)
            if from_request and result.get("success"):
                self._record_request_action(tool, arguments, result)
            executed.append(result)

        if pending.empty():
            self.pending = None
        else:
            # 일부만 승인했다. 남은 항목의 유효 기간을 지금부터 다시 센다.
            pending.turn = self.turn
            pending.created_at = time.time()

        lines = []
        if executed:
            lines.append("처리했습니다.")
        elif not stale and not blocked:
            lines.append("처리할 항목이 없었습니다.")

        # 이 경로에는 모델이 쓴 문장이 없다. 결과 보고를 반드시 붙인다.
        report = self._result_report(trace, always=True)

        if blocked:
            lines.append("아래는 지금은 처리할 수 없습니다.\n  "
                         + "\n  ".join(blocked))

        failed = any(not entry.get("success") for entry in executed)

        # ① 승인 내용이 달라진 것부터 다시 확인받는다.
        if stale and self.pending is None:
            note = ("승인하신 내용과 지금 상태가 달라져 실행하지 않았습니다. "
                    "바뀐 내용으로 다시 확인해 주세요.")
            # _open_pending 은 남는 것이 없으면 postponed 를 비운다.
            # 여기서는 아직 미뤄 둔 후속 작업을 잃으면 안 되므로 지키고 되돌린다.
            keep = self.postponed
            summary, problems = self._open_pending(stale, trace, note=note)
            if self.pending is not None:
                self.pending.from_request = from_request
            if self.postponed is None:
                self.postponed = keep
            if summary:
                lines.append(summary)
            elif problems:
                lines.append("다시 확인하려 했으나 처리할 수 없습니다.\n  "
                             + "\n  ".join(problems))
        elif stale:
            lines.append("일부 항목은 내용이 달라져 실행하지 않았습니다. "
                         "남은 확인을 마친 뒤 다시 요청해 주세요.")

        # ② 미뤄 둔 후속 작업을 이어간다. 앞 작업이 실패했으면 진행하지 않는다.
        elif self.pending is None and self.postponed and not failed and not blocked:
            items = self.postponed["items"]
            self.postponed = None
            summary, problems = self._open_pending(
                items, trace, note="이어서 부탁하신 작업입니다.")
            if self.pending is not None:
                self.pending.from_request = from_request
            if summary:
                lines.append(summary)
            elif problems:
                lines.append("이어서 진행하려던 작업은 처리할 수 없습니다.\n  "
                             + "\n  ".join(problems))

        if self.pending is not None and len(self.pending.items) > 1:
            lines.append(f"확인이 필요한 항목이 {len(self.pending.items)}건 있습니다.")

        if (failed or blocked) and self.postponed:
            # 앞 작업이 안 됐는데 뒤 작업을 진행하면 사용자가 원한 결과가 아니다.
            self.postponed = None
            lines.append("앞 작업이 완료되지 않아 이어지는 작업은 진행하지 않았습니다.")

        # 실행한 것이 있고 전부 성공했으며 막힌 것도 없을 때만 "승인된 작업이 처리되었다" 가 참이다.
        self._approval_outcome = {"ok": bool(executed) and not failed and not blocked and not stale,
                                  "from_request": from_request}
        answer = "\n\n".join(line for line in lines if line)
        return (answer + report).strip(), trace

    # 승인 뒤 이어갈 때 모델에게 보내는 문장. 사용자가 친 말이 아니므로
    # 서버는 이것을 대화 내역의 user 메시지로 남기지 않는다.
    RESUME_MESSAGE = ("(앱) 승인된 작업이 처리되었습니다. 사용자의 원래 요청은 다음과 같았습니다:\n"
                      "「{request}」\n"
                      "이 요청에서 이미 실행된 작업:\n{done}\n"
                      "이 요청 중 **아직 하지 않은 부탁**이 있으면 지금 이어서 처리하세요 "
                      "(예: 취소 뒤 다른 상품 담기, 반품 뒤 재고 확인). "
                      "위 목록에 있는 것은 다시 하지 마세요. 결제가 끝난 상품은 장바구니에서 빠지므로 "
                      "장바구니가 비어 있어도 담기를 다시 하지 마세요. 승인 전 답변에 '담았습니다'처럼 적었는데 "
                      "위 목록에 없는 일만 지금 실행하세요. "
                      "남은 부탁이 없으면 Tool 을 부르지 말고 한 문장으로만 마무리하세요.")
    def _record_request_action(self, tool, arguments, result):
        """이 요청에서 성공한 상태 변경을 남긴다. 승인 뒤 이어가기에서 같은 일을 다시 하지 않게 한다."""
        if not self.last_request or tool not in STATE_CHANGING:
            return
        clean = {key: value for key, value in (arguments or {}).items() if key != "confirm"}
        self.last_request.setdefault("done", []).append({
            "tool": tool, "arguments": clean,
            "message": (result.get("message") or tool).split("\n")[0][:160]})

    def _repeats_request_action(self, tool, arguments):
        """이어가기 중인 호출이 이 요청에서 이미 성공한 일을 되풀이하나.

        "첫 번째 거 M 으로 담고 결제해줘" 에서 결제 승인 뒤 이어가기가 빈 장바구니를 보고 같은 상품을
        다시 담고 결제했다(GLM 글 2/3·사진 2/3, Gemma·Qwen 도, 2026-10-06). 같은 상품·사이즈 담기와
        두 번째 결제를 막는다. 그 밖의 상태 변경은 인자가 같을 때만 막는다.
        """
        done = (self.last_request or {}).get("done") or []
        clean = {key: value for key, value in (arguments or {}).items() if key != "confirm"}
        if tool == "buy_from_cart":
            return any(entry["tool"] == "buy_from_cart" for entry in done)
        if tool == "add_to_cart":
            key = (clean.get("product_id"), str(clean.get("size")))
            return any(entry["tool"] == "add_to_cart"
                       and (entry["arguments"].get("product_id"), str(entry["arguments"].get("size"))) == key
                       for entry in done)
        return any(entry["tool"] == tool and entry["arguments"] == clean for entry in done)

    # 승인이 원래 요청과 같은 흐름인지 판단하는 턴 간격. run(요청) → approve 가 +1.
    RESUME_MAX_TURN_GAP = 2

    def continue_after_approval(self, history=None):
        """승인으로 끊긴 요청에 남은 부분이 있으면 모델을 다시 불러 이어간다.

        "어제 주문 취소하고 흰색 운동화 담아줘" 에서 취소는 승인 버튼으로 끊긴다.
        이것이 없을 때는 승인 뒤 뒷부분을 사용자가 다시 말해야만 진행됐다.
        원래 요청 문장을 들려주고 "남은 부탁이 있으면 이어가라" 고 한 번 더 부른다. 남은 게 없으면 모델이
        한 문장으로 마무리하므로 비용은 승인 한 번당 모델 호출 한 번이다.
        확인 대기·미룬 작업이 남아 있으면 그쪽 절차가 먼저이므로 부르지 않는다.
        돌려주는 값은 run() 과 같고, 이어갈 것이 없으면 None.
        """
        outcome = self._approval_outcome or {}
        # 한 번의 승인 결과로 한 번만 판단한다(같은 승인 요청이 두 번 와도 다시 잇지 않는다).
        self._approval_outcome = None
        if self.pending is not None or self.postponed:
            return None
        if not config.RESUME_AFTER_APPROVAL:
            return None
        # 방금 승인이 실제로 성공했고, 그 확인을 채팅 요청이 연 경우만 잇는다.
        # 실패·만료·빈 승인 뒤에 "승인된 작업이 처리되었다" 고 들려주면 모델이 뒤 작업을 진행한다.
        # 주문 화면 버튼으로 연 확인은 예전 채팅 요청과 관계가 없다.
        if not (outcome.get("ok") and outcome.get("from_request")):
            return None
        request = self.last_request
        if not request or self.turn - request["turn"] > self.RESUME_MAX_TURN_GAP:
            return None
        if not request.get("interrupted"):
            # 승인이 요청의 마지막 단계였다면(“결제해줘” 하나) 이어갈 것이 없다.
            return None
        # 이어가기는 한 번만. 이어간 run 은 "(앱)" 요청이라 last_request 를 새로 쓰지 않으므로
        # 여기서 끄지 않으면 다음 승인에서 같은 요청을 또 잇는다(같은 상품 중복 담기).
        request["interrupted"] = False
        # 이어간 run 은 Tool 을 하나도 안 부르고 "담았습니다" 로 끝낼 수 있다(승인 전 답변의 완료형을
        # 믿고). 그 경우도 검증기가 [현재 상태]와 원래 요청을 대조하도록 표시해 둔다.
        self._resuming = True
        # 원래 요청에 붙었던 사진 블록은 빼고 사용자 말만 들려준다. 블록째 넣으면 run 이 "사진을 새로
        # 올린 턴"으로 보고 검색 조건·아이템 선택을 지운다. 사진 ID 는 [현재 상태]의 사진 목록에 있다.
        text, photo_note, _ = request["text"].partition(_PHOTO_NOTE)
        done = "\n".join(f"- ✓ {entry['message']}" for entry in request.get("done") or []) or "- (없음)"
        try:
            return self.run(self.RESUME_MESSAGE.format(
                request=text + (" (사진 첨부)" if photo_note else ""), done=done), history)
        finally:
            self._resuming = False

    # 확인 대기가 열릴 때, 같은 턴에 조회한 내용을 사용자에게 먼저 답하게 하는 문장.
    ANSWER_BEFORE_CONFIRM = (
        "(앱) 확인이 필요한 작업은 앱이 버튼으로 처리합니다. 그것을 제외하고, "
        "지금까지 Tool 로 확인한 내용 중 사용자가 물은 것(재고·세탁·주문 상태 등)이 있으면 "
        "그 답만 두 문장 이내로 쓰세요. 확인 질문을 다시 쓰지 말고, Tool 도 부르지 마세요. "
        "답할 것이 없으면 빈 답을 보내세요.")

    def _answer_before_confirm(self, messages, trace, model_content):
        """확인 대기로 턴이 끊기기 전에, 이번 턴의 조회 결과를 답변으로 남긴다.

        "반품 신청하고 105 사이즈 재고도 알려줘" 에서 모델은 get_info 로 재고를
        확인했지만, 반품 확인 대기가 열리면서 그 답을 쓸 기회가 사라졌다.
        조회 Tool 이 이번 턴에 실행됐고 모델이 아직 문장을 쓰지 않았다면
        모델을 한 번 더 불러 조회 결과만 답하게 한다. 그 외에는 부르지 않는다.
        """
        if model_content:
            return model_content.strip()
        informational = [
            entry for entry in trace
            if entry.get("tool") in INFORMATIONAL_TOOLS and not entry.get("internal")
            and (entry.get("result") or {}).get("success")
        ]
        if not informational:
            return ""
        try:
            message = call_model(
                messages + [{"role": "user", "content": self.ANSWER_BEFORE_CONFIRM}],
                tools=None)
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return ""
        if parse_tool_calls(message):
            return ""
        return (message.get("content") or "").strip()

    def reject(self):
        """확인을 거절했다.

        **이번 요청에서 이미 실행된 작업까지 되돌리지는 않는다.**
        "아무것도 변경하지 않았습니다" 라고 안내하면 거짓말이 된다.
        ("B 담고 A 빼줘" 에서 B 는 이미 담긴 상태다)
        거절한 범위만 정확히 말한다.
        """
        if self.pending is None:
            return "확인 대기 중인 작업이 없습니다.", []

        self.turn += 1
        labels = [item["label"] for item in self.pending.items]
        tool = self.pending.tool
        had_postponed = bool(self.postponed)

        self.pending = None
        self.postponed = None

        lines = [f"대기 중이던 {ACTION_NAME.get(tool, tool)} {len(labels)}건을 "
                 f"진행하지 않았습니다."]
        if had_postponed:
            lines.append("이어서 하려던 작업도 함께 중단했습니다.")
        lines.append("이미 처리된 작업은 그대로 남아 있습니다.")
        return "\n\n".join(lines), []

    # ------------------------------------------------------------------
    # 상태 저장/복원 — 서버가 요청 사이에 DB 에 둔다
    #
    # HTTP 서버는 요청마다 다른 스레드에서 이 객체를 부르고, 재시작하면 객체가
    # 사라진다. 확인 대기가 객체 안에만 있으면 재시작 뒤 화면의 승인 버튼이
    # 아무것도 가리키지 않는다. 그래서 요청이 끝날 때마다 아래 dict 를 저장하고,
    # 객체를 새로 만들 때 되살린다. 정책(만료·열쇠 비교)은 그대로 이 파일이 한다.
    # ------------------------------------------------------------------

    def snapshot(self):
        """저장할 상태. 전부 JSON 으로 표현 가능한 값만."""
        return {
            "turn": self.turn,
            "pending": self.pending.to_dict() if self.pending is not None else None,
            "postponed": self.postponed,
            "last_results": self.last_results,
            "last_search": self.last_search,
            "search_intake": self.search_intake,
            "last_image_analysis": self.last_image_analysis,
            "last_request": self.last_request,
            "last_orders": self.last_orders,
            "photos": self.photos,
            "last_photo_gender": self.last_photo_gender,
            "preferences": self.preferences.to_dict(),
        }

    def restore(self, state):
        """snapshot() 이 만든 dict 로 되살린다. 만료된 확인 대기는 살리지 않는다."""
        if not state:
            return
        self.turn = int(state.get("turn") or 0)
        pending = state.get("pending")
        self.pending = PendingAction.from_dict(pending) if pending else None
        if self.pending is not None and self.pending.expired():
            self.pending = None
            # 이 작업들은 만료된 승인을 전제로 뒤에 이어질 예정이었다.
            # 앞 승인이 무효가 됐으므로 함께 버려야 나중에 되살아나지 않는다.
            self.postponed = None
        else:
            self.postponed = state.get("postponed") or None
        self.last_results = state.get("last_results") or None
        self.last_search = state.get("last_search") or None
        self.search_intake = state.get("search_intake") or None
        self.last_image_analysis = state.get("last_image_analysis") or None
        self.last_request = state.get("last_request") or None
        self.last_orders = state.get("last_orders") or None
        self.photos = list(state.get("photos") or [])
        self.last_photo_gender = state.get("last_photo_gender")
        self.preferences = agency.PreferenceMemory.from_dict(state.get("preferences"))

    def model_request_parts(self, image_tools=True):
        """메인 모델 호출의 (시스템 프롬프트, 도구 목록).

        도구 목록은 두 가지뿐이다(사진 Tool 포함/제외). 서버의 앞부분 캐시가 두 벌이면 되므로
        턴마다 목록을 바꾸는 것보다 캐시가 오래 산다.
        """
        system_prompt = SYSTEM_PROMPT
        tools = [tool for tool in MODEL_TOOLS
                 if image_tools or tool["function"]["name"] not in IMAGE_TOOLS]
        if config.ADAPTIVE_AGENT_MODE:
            system_prompt += agency.ADAPTIVE_PROMPT
        if config.USER_MEMORY_ENABLED:
            system_prompt += agency.MEMORY_PROMPT
            tools.append(agency.MEMORY_TOOL)
        return system_prompt, tools

    def _substituted_size(self, arguments, trace):
        """사이즈가 없어 담기가 거절된 상품을, 사용자가 말하지 않은 다른 사이즈로 다시 담으려는가.

        "레깅스 66" -> L 로 시도해 거절 -> 모델이 목록에서 28 을 골라 담았다. 사용자는 28 을
        고른 적이 없다. 이 경우만 막는다 — 말하지 않은 사이즈를 전부 막으면 "같은 사이즈로",
        저장된 선호 사이즈 같은 정상 요청이 깨진다. 막을 때는 모델에게 되물으라는 문장을 준다.
        """
        if arguments.get("size") is None:
            return None
        refused = [(entry.get("result") or {}).get("data") or {} for entry in trace
                   if entry.get("tool") == "add_to_cart" and not entry.get("internal")]
        refused = [data for data in refused if data.get("size_unavailable")]
        if not refused:
            return None
        product, error = self.toolbox._resolve_product(arguments.get("product_id"),
                                                       arguments.get("product_name"))
        if error:
            return None            # 상품을 못 찾으면 Tool 이 알아서 실패시킨다
        try:
            size = normalize_size(arguments["size"])
        except ValueError:
            return None
        said = _sizes_mentioned(self.toolbox.user_text or "")
        for data in refused:
            if (data.get("product_id") == product["id"]
                    and size != data.get("requested_size") and size not in said):
                stock = ", ".join(map(str, data.get("in_stock_sizes") or [])) or "없음"
                return (f"사용자는 {data.get('requested_size')} 사이즈를 요청했고 이 상품에는 그 사이즈가 "
                        f"없습니다. 사용자가 말하지 않은 {size} 로 대신 담지 않았습니다. "
                        f"재고 있는 사이즈({stock})를 알려 주고 어느 것으로 담을지 물으세요.")
        return None

    def _photo_gender(self, call, cleaned):
        """새 사진 검색의 성별은 이번 사진 이후 사용자 말(또는 저장된 선호)에 근거가 있을 때만 쓴다.

        색·품목·소재는 filter_resolution 이 근거를 보지만 성별은 보지 않았다. GLM 이 두 번째 사진에서
        첫 사진의 "남성용"을 묻지 않고 그대로 썼다(2026-10-06). 근거 없이 넘어온 성별은 빼서 앱이 다시
        묻게 하고, 그 값을 "이번에도 ○○으로 찾을까요?" 제안으로 돌려준다. 사용자가 그 제안에 "응"이라고
        하면 그 성별로 채운다. 반환: (검사용 인자, 제안할 성별).
        """
        if call.name != "search_by_image_and_text":
            return cleaned, None
        texts = self.toolbox.user_texts or []
        if any(GENDER_EVIDENCE.search(text or "") for text in texts):
            return cleaned, None
        # 같은 사진에서 이미 정한 성별은 다시 묻지 않는다. 첫 사진의 모자를 나중에 찾을 때도
        # "이번에도 남성용으로?"를 물었다(3모델 모두, 2026-10-06). 묻는 건 새 사진일 때만이다.
        confirmed = (self._photo_of(call.arguments if isinstance(call.arguments, dict) else {}) or {}).get("gender")
        if confirmed and cleaned.get("gender") in (None, "", confirmed):
            if not cleaned.get("gender") and isinstance(call.arguments, dict):
                call.arguments = {**call.arguments, "gender": confirmed}
            return {**cleaned, "gender": confirmed}, None
        latest, asked = (self.photo_answers or [("", "")])[0]
        if self.last_photo_gender and "이번에도" in (asked or "") and _AFFIRMATIVE.fullmatch(latest or ""):
            # "이번에도 남성용으로 찾을까요?" → "응". 모델이 성별을 빼먹어도 채운다.
            if not cleaned.get("gender") and isinstance(call.arguments, dict):
                call.arguments = {**call.arguments, "gender": self.last_photo_gender}
            return {**cleaned, "gender": cleaned.get("gender") or self.last_photo_gender}, None
        saved = (self.preferences.public().get("gender") or [None])[0] if config.USER_MEMORY_ENABLED else None
        gender = cleaned.get("gender")
        if not gender or gender == saved:
            return cleaned, None
        return {key: value for key, value in cleaned.items() if key != "gender"}, gender

    def _with_chosen_item(self, arguments):
        """사용자가 고른 사진 아이템을 검색 인자에 이어 붙인다.

        아이템을 고르는 질문은 모델이 한다. 사용자가 "1번"이라고 답한 뒤 모델이 item_id 를 빼고
        search_by_image_and_text 를 부르면 서버는 아이템 3개를 보고 다시 "선택하세요" 를 돌려줬다
        (2026-09-30 세 번 재현). 그래서 사용자 답을 후보와 맞춰 하나로 분명할 때만 채운다.
        모델이 넣은 item_id 는 건드리지 않고, 다른 analysis_id(다른 사진)도 건드리지 않는다.
        """
        if arguments.get("item_id"):
            return arguments
        context = self.last_image_analysis or {}
        analysis_id = arguments.get("analysis_id") or context.get("analysis_id")
        if not analysis_id or (context and analysis_id != context.get("analysis_id")):
            return arguments
        items = context.get("items")
        if not items:
            # 모델이 검증 블록의 "아이템 N개"만 보고 직접 물었으면 검색 실패 기록이 없다.
            import image_query_service
            import shopping_image_analysis
            try:
                items = shopping_image_analysis.load_analysis(
                    self.toolbox.store.user_id, analysis_id).get("items")
            except image_query_service.ImageQueryError:
                return arguments
        if not items or len(items) < 2:
            return arguments
        chosen = _answered_item(items, self.photo_answers)
        if chosen is None:
            return arguments
        return {**arguments, "analysis_id": analysis_id, "item_id": chosen["item_id"]}

    def run(self, user_message, history=None, image_data_url=None):
        """사용자 메시지 하나를 처리하고 (최종답변, 실행기록) 을 반환한다.

        trace 는 "에이전트가 어떤 Tool 을 어떤 인자로 불렀는지" 의 기록이다.
        상담창이 이것을 펼쳐 어떤 Tool 을 어떤 인자로 불렀는지 보여 준다.
        """
        history = history or []
        trace = []
        verifier_retries = 0
        fabrication_retried = False
        photo_nudged = False
        photo_forced = False
        forced_tools = None
        button_nudged = False
        compare_nudged = False
        empty_retried = False
        is_resume = user_message.startswith("(앱) ")
        # 툴이 "사용자가 직접 말한 값인지"를 판정할 원문. 서버가 붙인 검증 블록은 뺀다 —
        # 거기 적힌 VLM 추정값("초록색")이 사용자 말로 오인되면 하드 필터 보호가 무력화된다.
        self.toolbox.user_text = user_message.split(_PHOTO_NOTE, 1)[0]
        # 이전 턴에 말한 조건("검은 니트" → 다음 턴 "좀 더 싼 걸로")도 사용자가 말한 것이다.
        self.toolbox.user_texts = filter_resolution.user_turns(user_message, history)
        self.photo_answers = _photo_answers(user_message, history)
        failed_signatures = set()
        completed_mutations = set()
        self.turn += 1

        # 오래된 확인 대기는 버린다.
        #
        # 그대로 두면 승인 버튼이 화면에 계속 떠 있다가 한참 뒤에 눌린다.
        # 그때는 미리보기가 계산된 시점의 장바구니·주문 상태가 아니다.
        if self.pending is not None and (
                self.turn - self.pending.turn > PENDING_TTL_TURNS
                or self.pending.expired()):
            self.pending = None
            self.postponed = None

        # 이 턴에 사진이 왔나(블록을 못 읽어도). 사진 Tool 을 모델에게 보일지 정한다.
        new_photo = bool(image_data_url or _PHOTO_NOTE in user_message)
        if new_photo:
            # 새 사진은 새 검색이다. 앞 사진의 아이템 선택·확인 중이던 검색 조건을 잇지 않는다
            # (user_turns 도 이 턴에서 멈춘다). 남겨 두면 _with_chosen_item 이 analysis_id 없는
            # 호출에 앞 사진의 item_id 를 붙일 수 있다.
            self.last_image_analysis = None
            self.search_intake = None
            record = _photo_record(user_message)
            if record:
                self.photos = [p for p in self.photos if p["query_image_id"] != record["query_image_id"]]
                self.photos.append(record)
                self.photos = self.photos[-MAX_PHOTOS_IN_STATE:]
        image_tools = bool(
            self.last_image_analysis
            or (self.search_intake or {}).get("tool") == "search_by_image_and_text"
            or new_photo
            or self._active_photos())
        said = self.toolbox.user_text or ""
        if (image_tools and "사진" in said and _PHOTO_CONTENT_QUESTION.search(said)
                and not _SEARCH_INTENT.search(said)
                and not self.search_intake and not self.last_image_analysis):
            # "이 사진에 뭐가 있어?" 는 검색이 아니다. 사진 이해 블록으로 답하면 된다. GLM 은 사진 검색을
            # 불러 성별을 물었다(2/3, 2026-10-06). 이 턴만 사진 검색 툴을 빼서 고를 수 없게 한다.
            # 사진 검색의 되묻기에 답하는 중("무슨 조건을 말하면 돼?")에는 막지 않는다.
            # 툴을 목록에서 빼면 GLM 이 없는 툴을 부르려다 서버 파서가 버려 빈 답이 났다(2/6) — 툴은 두고
            # 검색 호출만 막는다(아래 content_only).
            content_only = True
        else:
            content_only = False
        content_blocks = 0
        system_prompt, tools = self.model_request_parts(image_tools=image_tools)

        messages = [
            {"role": "system", "content": system_prompt},        # 고정. 캐시가 재사용한다
            *history,
            self._user_message(user_message, image_data_url),    # 현재 상태 + 질문 + 현재 사진
        ]
        if not is_resume:
            # done: 이 요청에서 성공한 상태 변경(담기·결제·취소…). 승인 뒤 이어가기가 같은 일을 다시 하지
            # 않게 하는 근거다(_repeats_request_action).
            self.last_request = {"text": user_message[:500], "turn": self.turn,
                                 "interrupted": False, "done": []}

        for _ in range(config.MAX_TOOL_ITERATIONS):
            # --- ① 모델에게 대화 + Tool 목록을 보낸다 ---
            try:
                message = call_model(messages, tools=forced_tools or tools)
                forced_tools = None
            except requests.Timeout:
                return ("모델 응답 시간이 초과되었습니다. 서버 상태를 확인해 주세요."
                        + self._result_report(trace, interrupted=True)), trace
            except requests.ConnectionError:
                return ("로컬 모델 서버에 연결할 수 없습니다. 서버 실행 여부를 확인해 주세요."
                        + self._result_report(trace, interrupted=True)), trace
            except requests.HTTPError as error:
                status = error.response.status_code if error.response is not None else "알 수 없음"
                return (f"모델 요청에 실패했습니다. HTTP 상태: {status}"
                        + self._result_report(trace, interrupted=True)), trace
            except requests.RequestException:
                return ("모델 요청 중 통신 오류가 났습니다. 잠시 뒤 다시 시도해 주세요."
                        + self._result_report(trace, interrupted=True)), trace
            except (KeyError, IndexError, ValueError, TypeError):
                return ("모델 응답 형식을 읽지 못했습니다. call_model() 의 파싱 부분을 확인하세요."
                        + self._result_report(trace, interrupted=True)), trace

            # --- ② 모델이 Tool 을 요청했는지 확인한다 ---
            calls = parse_tool_calls(message)

            # 사진 내용만 물었는데 검색하려 한다. 성별 되묻기 같은 검색 전 확인보다 먼저 막는다.
            if content_only and any(call.name in ("search_by_image_and_text", "search_product") for call in calls):
                content_blocks += 1
                if content_blocks >= 2:
                    # 두 번 막아도 검색하려 한다. 사진 분석 결과로 앱이 답한다.
                    return self._photo_contents_answer(), trace
                messages.append(_assistant_message(message, calls))
                for call in calls:
                    result = {"success": False, "data": None, "message": PHOTO_CONTENT_NOTE}
                    trace.append({"tool": call.name, "arguments": call.arguments,
                                  "result": result, "internal": True})
                    messages.append(make_tool_result_message(call, result))
                continue

            # 한 응답의 검색·담기 묶음에서 필수 조건이 빠졌다면 어떤 호출도 먼저 실행하지 않는다.
            # 모델이 안내를 무시하고 같은 턴에 조건을 지어내 재검색하는 루프도 여기서 끊는다.
            for call in calls:
                if call.error:
                    continue
                cleaned, error = validate_call(call.name, call.arguments)
                if error:
                    continue
                cleaned = fill_category_from_user(call.name, cleaned, self.toolbox.user_texts)
                cleaned, carried = self._photo_gender(call, cleaned)
                clarification = search_clarification(
                    call.name, cleaned,
                    previous_gender=carried or (self.last_photo_gender
                                                if call.name == "search_by_image_and_text" else None))
                if clarification:
                    self.search_intake = {
                        "tool": call.name, "arguments": cleaned,
                        "missing_fields": clarification["data"]["missing_fields"],
                        "request": (self.search_intake or {}).get("request", self.toolbox.user_text),
                    }
                    trace.append({"tool": call.name, "arguments": cleaned, "result": clarification})
                    return (clarification["message"] + self._result_report(trace, interrupted=True)
                            + self._stale_pending_note()), trace

            # 요청이 없으면 그게 최종 답변이다. 루프 끝.
            if not calls:
                answer = (message.get("content") or "").strip()
                if not answer and not empty_retried:
                    # 툴도 답도 없이 끝났다. GLM 은 가끔(1/3) 추론만 하고 본문 없이 멈췄고, Nemotron 은 추론이 출력
                    # 상한을 다 썼다(2026-10-07). 한 번은 답을 쓰게 다시 부른다.
                    empty_retried = True
                    messages.append({"role": "user", "content": EMPTY_ANSWER_NUDGE})
                    continue
                if not answer:
                    answer = "답변을 생성하지 못했습니다. 다시 질문해 주시겠어요?"
                verifier_asked = False
                verifier_question = None

                # 툴이 하나도 성공하지 않은 턴에 검색 결과처럼 쓴 답은 지어낸 것이다(앞 대화의 앱 문장 흉내).
                # 한 번은 검색 툴을 부르라고 다시 시키고, 그래도 지어내면 결과 없이 사과한다.
                if not any((entry.get("result") or {}).get("success") for entry in trace
                           if not entry.get("internal")) \
                        and _looks_like_search_result(answer):
                    if not fabrication_retried:
                        fabrication_retried = True
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": FABRICATED_RESULT_NUDGE})
                        if new_photo:
                            # 방금 사진을 올린 턴에 결과를 지어냈다. 말로 시키면 Qwen 은 "다시 찾아볼까요?"로
                            # 되물어 검색이 한 턴 밀렸다(2026-10-07). 사진 검색 툴 하나만 주고 부르게 한다.
                            forced_tools = [tool for tool in tools if (tool.get("function") or {}).get("name")
                                            == "search_by_image_and_text"] or None
                        continue
                    return (FABRICATED_RESULT_ANSWER + self._result_report(trace)
                            + self._stale_pending_note()), trace

                # 사진 검색 중인데 툴을 부르기 전에 모델이 직접 아이템·성별을 물었다. 크기 규칙(서버)을
                # 건너뛰게 되어 모델·실행마다 동작이 달랐다(Qwen 2/3, GLM 3/3 이 니트가 압도적인 사진에서도
                # 물음). 한 번은 툴을 먼저 부르라고 하고, 그래도 물으면 사진 검색 툴 하나만 주고 꼭 부르게
                # 한다(tool_choice=required). 말로만 다시 시키면 Qwen 이 또 물었다(1/2, 2026-10-06).
                # 강제 호출에서도 툴을 안 부르면 그 질문을 그대로 둔다.
                if (image_tools and not photo_forced
                        and not any(entry.get("tool") == "search_by_image_and_text" for entry in trace)
                        and not _PHOTO_CONTENT_QUESTION.search(self.toolbox.user_text or "")
                        and _asks_photo_choice(answer)):
                    if photo_nudged:
                        photo_forced = True
                        forced_tools = [tool for tool in tools
                                        if (tool.get("function") or {}).get("name") == "search_by_image_and_text"]
                    photo_nudged = True
                    messages.append({"role": "assistant", "content": answer})
                    messages.append({"role": "user", "content": PHOTO_TOOL_FIRST_NUDGE})
                    continue

                # 툴을 부르지 않고 "승인 버튼을 눌러 주세요"라고 썼다. 버튼은 확인이 필요한 툴을 불러야만
                # 생기므로 사용자는 누를 버튼이 없다(GLM·Qwen, 2026-10-06). 한 번은 툴을 부르게 하고,
                # 그래도 같으면 버튼이 없다고 덧붙인다.
                if (self.pending is None and _BUTTON_CLAIM.search(answer)
                        and _ACTION_REQUEST.search(self.toolbox.user_text or "")):
                    if not button_nudged:
                        button_nudged = True
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": BUTTON_CLAIM_NUDGE})
                        continue
                    answer = f"{answer}\n\n{BUTTON_CLAIM_NOTE}"

                external = [entry for entry in trace
                            if not entry.get("internal")
                            and entry.get("tool") != "manage_preferences"]
                if config.ADAPTIVE_AGENT_MODE and (self._resuming or (
                        external and _needs_verification(self.toolbox.user_text, external))):
                    decision = self._verify_outcome(user_message, answer, trace)
                    if decision and _chases_other_photo_item(decision, trace, self.toolbox.user_text):
                        decision = None
                    if decision and decision["verdict"] == "ask_user":
                        answer = decision["question"]
                        verifier_asked = True
                        verifier_question = answer
                    elif (decision and decision["verdict"] == "retry"
                          and verifier_retries < config.MAX_VERIFIER_RETRIES):
                        changed = [entry for entry in external
                                   if entry.get("tool") in STATE_CHANGING
                                   and (entry.get("result") or {}).get("success")]
                        if changed:
                            missing = ", ".join(decision["missing_requirements"])
                            answer = (f"{answer}\n\n일부 작업이 이미 반영되어 자동으로 다시 "
                                      f"실행하지 않았습니다. 아직 확인할 내용: {missing}. "
                                      "계속 진행할지 알려주세요.")
                            verifier_asked = True
                        else:
                            verifier_retries += 1
                            messages.append({"role": "assistant", "content": answer})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "[결과 검증] 아직 충족되지 않은 요구가 있습니다: "
                                    + "; ".join(decision["missing_requirements"])
                                    + "\n다음 지시만 안전하게 보완하세요: "
                                    + decision["next_instruction"]
                                    + "\n이미 성공한 상태 변경 Tool은 다시 실행하지 말고, "
                                      "사용자가 명시한 조건을 임의로 완화하지 마세요."
                                ),
                            })
                            continue
                    elif decision and decision["verdict"] == "retry":
                        missing = ", ".join(decision["missing_requirements"])
                        answer = (f"{answer}\n\n다만 자동 보완 횟수를 모두 사용했고 "
                                  f"아직 확인하지 못한 조건이 있습니다: {missing}. "
                                  "필요한 조건을 조금 더 구체적으로 알려주세요.")
                        verifier_asked = True
                if not verifier_asked:
                    answer = _search_grid_reply(trace, answer, user_message,
                                                self._photo_label(trace)) or answer
                elif verifier_question:
                    # 검색은 성공했는데 검증기가 되물었다. 예전에는 질문이 답 전체를 바꿔서 화면에는 셔츠 50개가
                    # 떴는데 채팅에는 "팬츠도 찾아드릴까요?"만 남았다(GLM 2/2, 2026-10-06). 목록은 두고 질문을
                    # 붙인다. 사진 답이 이미 "팬츠도 있어요"라고 알리면 같은 말이라 붙이지 않는다.
                    grid = _search_grid_reply(trace, "", user_message, self._photo_label(trace))
                    if grid:
                        answer = grid if _offered_other_items(trace) else f"{grid}\n\n{verifier_question}"
                # 모델이 쓴 문장 아래에 앱이 만든 사실을 붙인다.
                return answer + self._result_report(trace) + self._stale_pending_note(), trace

            # --- ③ 모델이 무엇을 요청했는지 대화에 남긴다 ---
            messages.append(_assistant_message(message, calls))

            # --- ④ 요청서를 보고 우리가 대신 실행한다 ---
            #
            # 확인이 필요한 작업은 한 턴에 하나만 처리한다.
            #
            # 전에는 여러 건을 한 승인으로 묶었는데, 그게 세 가지 문제를 냈다.
            #   - 서로 다른 Tool 을 묶으면 승인 때 어느 Tool 을 부를지 정해지지 않는다.
            #     ("1개 빼고 주문까지" -> pending 은 하나인데 모델은 둘 중 하나만 부른다)
            #   - 뒤 작업의 미리보기가 앞 작업 전의 상태로 계산된다.
            #     (1개 빼기 전 장바구니로 결제 금액을 뽑아 틀린 금액을 보여줬다)
            #   - 주문 두 건 취소처럼 한 호출로 표현할 수 없는 작업은
            #     승인이 영영 성립하지 않아 미리보기만 반복됐다.
            #
            # 여러 대상을 한 번에 처리해야 하면 Tool 이 그것을 받아야 한다
            # (remove_from_cart 의 items). 그건 호출 하나이므로 확인도 한 번이다.
            holding = None        # 이번 턴에 확인을 기다리게 된 작업
                                  # {"tool": 이름, "items": [...], "rows": [...]}
            postponed = []        # 확인이 하나 잡혀서 이번 턴에 미룬 작업들

            for call in calls:
                # 인자를 읽지 못한 호출은 실행하지 않고 사유만 돌려준다.
                if call.error:
                    result = {"success": False, "data": None,
                              "message": f"{call.name}: {call.error}"}
                    trace.append({"tool": call.name, "arguments": {}, "result": result})
                    # 실패 서명에 넣지 않는다. 인자 없는 서명이라, 모델이 인자를 고쳐 다시
                    # 불러도 인자 없는 정상 호출(view_cart() 등)까지 "이미 실패" 로 막힌다.
                    messages.append(make_tool_result_message(call, result))
                    continue

                arguments = call.arguments if isinstance(call.arguments, dict) else {}

                if call.name == "manage_preferences":
                    # 기억 기능을 끄면 도구 목록에서도 빠진다. 그래도 모델이 이 이름을 부르면
                    # 저장하지 않는다.
                    result = (self.preferences.update(arguments) if config.USER_MEMORY_ENABLED
                              else {"success": False, "data": None,
                                    "message": "선호 기억 기능이 꺼져 있어 저장하지 않았습니다."})
                    trace.append({"tool": call.name, "arguments": arguments,
                                  "result": result})
                    messages.append(make_tool_result_message(call, result))
                    continue

                if call.name == "search_by_image_and_text":
                    # 사용자가 고른 사진 아이템을 모델이 빠뜨렸으면 앱이 채운다.
                    arguments = self._with_chosen_item(arguments)

                signature = self._call_signature(call.name, arguments)
                if (call.name == "add_to_cart" and not compare_nudged and not is_resume
                        and "비교" in (self.toolbox.user_text or "")
                        and (self.last_results or {}).get("tool") != "comparing_info"
                        and not any(entry.get("tool") == "comparing_info"
                                    and (entry.get("result") or {}).get("success") for entry in trace)):
                    # "3개 비교해서 리뷰 많은 거 담아줘" 에서 Qwen 이 비교 없이 1위를 담았다(1/3). 한 번은
                    # 비교부터 하게 한다. 그래도 담으면 따른다(사용자가 고른 상품일 수 있다).
                    compare_nudged = True
                    result = {"success": False, "data": None, "message": COMPARE_FIRST_NOTE}
                    trace.append({"tool": call.name, "arguments": arguments,
                                  "result": result, "internal": True})
                    messages.append(make_tool_result_message(call, result))
                    continue
                if is_resume and self._repeats_request_action(call.name, arguments):
                    result = {
                        "success": False,
                        "data": None,
                        "message": ("이 요청에서 이미 성공한 작업입니다(결제가 끝난 상품은 장바구니에서 "
                                    "빠집니다). 다시 하지 말고, 남은 부탁이 없으면 한 문장으로 마무리하세요."),
                    }
                    trace.append({"tool": call.name, "arguments": arguments,
                                  "result": result, "internal": True})
                    messages.append(make_tool_result_message(call, result))
                    continue
                if signature in completed_mutations:
                    result = {
                        "success": False,
                        "data": None,
                        "message": ("같은 상태 변경은 이번 요청에서 이미 성공했습니다. "
                                    "중복 실행하지 말고 다음 단계로 진행하세요."),
                    }
                    trace.append({"tool": call.name, "arguments": arguments,
                                  "result": result, "internal": True})
                    messages.append(make_tool_result_message(call, result))
                    continue
                if signature in failed_signatures:
                    result = {
                        "success": False,
                        "data": None,
                        "message": ("같은 인자의 호출이 이미 실패했습니다. 같은 호출을 반복하지 말고 "
                                    "유효한 인자로 고치거나 사용자에게 필요한 값을 물으세요."),
                    }
                    trace.append({"tool": call.name, "arguments": arguments,
                                  "result": result, "internal": True})
                    messages.append(make_tool_result_message(call, result))
                    continue

                if call.name == "add_to_cart":
                    refusal = self._substituted_size(arguments, trace)
                    if refusal:
                        result = {"success": False, "data": None, "message": refusal}
                        trace.append({"tool": call.name, "arguments": arguments,
                                      "result": result, "internal": True})
                        messages.append(make_tool_result_message(call, result))
                        continue

                if call.name in CONFIRM_REQUIRED:
                    # 이미 확인 대기가 잡혔는데 **다른** Tool 이면 미룬다.
                    # 앞 작업이 반영되기 전 값으로 미리보기를 만들면 틀린 숫자가 나온다.
                    # ("1개 빼고 주문" 에서 빼기 전 금액으로 결제 금액을 뽑았다)
                    #
                    # 같은 Tool 이면 함께 확인한다. 서로 독립적인 대상이라
                    # 앞 건이 뒤 건의 미리보기를 바꾸지 않기 때문이다.
                    # ("두 주문 모두 취소" 를 한 번의 확인으로 끝낼 수 있다)
                    if holding is not None and holding["tool"] != call.name:
                        postponed.append({"tool": call.name, "arguments": arguments})
                        messages.append(make_tool_result_message(call, _POSTPONED_RESULT))
                        continue

                    # 한 번에 너무 많이 묶지 않는다.
                    # 확인 문장이 길어지면 사용자가 대충 읽고 승인하게 된다.
                    if holding is not None and len(holding["items"]) >= MAX_CONFIRM_AT_ONCE:
                        postponed.append({"tool": call.name, "arguments": arguments})
                        messages.append(make_tool_result_message(call, _POSTPONED_RESULT))
                        continue

                    # 미리보기를 돌려 "무엇을 처리할 것인가" 를 확정한다.
                    # preview 는 상태를 바꾸지 않으므로 매번 불러도 안전하고,
                    # 모델이 이름으로 부르든 ID 로 부르든 같은 답이 나온다.
                    preview_args = {**arguments, "confirm": False}
                    preview = self.toolbox.call(call.name, preview_args)

                    if not preview.get("success"):
                        # 미리보기 자체가 실패(장바구니에 없음, 취소 불가 등)면 모델에게 돌려준다.
                        trace.append({"tool": call.name, "arguments": preview_args,
                                      "result": preview})
                        failed_signatures.add(signature)
                        messages.append(make_tool_result_message(call, preview))
                        continue

                    # 앞서 묶은 것과 대상이 겹치면 함께 확인하지 않는다.
                    #
                    # 미리보기는 둘 다 "지금 장바구니" 로 계산되는데 실행은 순서대로다.
                    # 겹치면 앞 건이 뒤 건의 대상을 먹어버려, 승인한 것과 결과가 달라진다.
                    #   "2개 빼고 2개 더" -> 3개뿐인데 4개를 승인받는다
                    #   "다 빼고 225도 1개" -> 뒤 건이 "장바구니에 없습니다" 로 실패한다
                    # 묶는 기준은 "Tool 이 같은가" 가 아니라 "대상이 겹치지 않는가" 다.
                    if holding is not None and _overlaps(holding["rows"], preview):
                        postponed.append({"tool": call.name, "arguments": arguments})
                        messages.append(make_tool_result_message(call, _POSTPONED_RESULT))
                        continue

                    # 승인은 버튼으로만 받으므로 여기서는 미리보기까지만. 모델이 confirm=True 를
                    # 보냈어도 실행되지 않는다.
                    trace.append({"tool": call.name, "arguments": preview_args,
                                  "result": preview})
                    entry, snap = self._entry({k: v for k, v in arguments.items() if k != "confirm"},
                                              preview, call.name)
                    if holding is None:
                        holding = {"tool": call.name, "items": [entry],
                                   "rows": list(snap["rows"])}
                    else:
                        holding["items"].append(entry)
                        holding["rows"].extend(snap["rows"])
                    # 모든 tool_call 에는 결과가 짝지어져야 한다. 검증기 retry 로 루프가
                    # 이어지면 결과 없는 tool_call 이 대화에 남아 모델이 같은 호출을 되풀이한다.
                    messages.append(make_tool_result_message(call, {
                        **preview, "message": (preview.get("message") or "")
                        + "\n(아직 실행 전입니다. 사용자 확인을 기다립니다.)"}))
                    continue

                result = self.toolbox.call(call.name, arguments)
                if call.name in {"search_product", "search_by_image_and_text"} and result.get("success"):
                    self.search_intake = None

                trace.append({"tool": call.name, "arguments": arguments, "result": result})
                self._remember_results(call.name, result, arguments)
                if not result.get("success"):
                    failed_signatures.add(signature)
                elif call.name in STATE_CHANGING:
                    completed_mutations.add(signature)
                    self._record_request_action(call.name, arguments, result)

                # --- ⑤ 결과를 대화에 덧붙인다 ---
                messages.append(make_tool_result_message(call, result))

            # 확인 대기가 생겼으면 여기서 멈추고 사용자 답을 기다린다.
            if holding:
                items = holding["items"]

                # --- 확인 버튼을 띄우기 전에 한 번 검증한다 ---
                # 최종 답 직전의 검증기는 확인 대기로 끝나는 턴에는 돌지 않았다. 그래서
                # "둘 다 결제" 에서 담기 하나가 재고로 실패한 채 결제 확인이 그대로 떴다.
                # 아직 아무것도 실행되지 않은 시점이므로 retry 가 안전하다: 확인 대기를 열지 않고
                # 모델에게 보완 지시를 준 뒤 루프를 이어간다. 비용을 위해 이번 턴에 실패한
                # Tool 이 있을 때만 돈다.
                # 실패로 세는 것: 재고 없음·잘못된 인자처럼 **사용자 의도가 미달된** 실패만.
                # 취소 불가·반품 기간 지남은 store 의 정책 판정(정상 거절)이라 세지 않는다 —
                # "취소 안 되면 이유 알려줘" 의 정상 거절을 실패로 세었을 때 검증기가 돌고,
                # retry 가 답 순서를 흔들어 배송 완료 목록이 빠졌다(3/3 회귀).
                failed_here = [entry for entry in trace
                               if not entry.get("internal")
                               and entry.get("tool") != holding["tool"]
                               and entry.get("tool") not in CONFIRM_REQUIRED
                               and not (entry.get("result") or {}).get("success")]
                if (config.ADAPTIVE_AGENT_MODE and config.VERIFY_BEFORE_CONFIRM and failed_here
                        and verifier_retries < config.MAX_VERIFIER_RETRIES):
                    labels = [item["label"] for item in items]
                    decision = self._verify_outcome(user_message, "", trace, about_to_confirm=labels)
                    # 결제 직전에는 retry 를 허용하지 않는다. retry 지시를 받은 모델이
                    # 사용자가 고르지 않은 다른 상품을 대신 담아 2건(411,000원)을 결제한 일이 있었다.
                    # 결제 앞의 "보완" 은 반드시 사용자에게 묻는 것으로만 한다.
                    if (decision and decision["verdict"] == "retry" and holding["tool"] == "buy_from_cart"):
                        question = ("요청하신 상품 중 담지 못한 것이 있습니다: "
                                    + "; ".join(decision["missing_requirements"])
                                    + "\n다른 상품으로 대신 담을까요, 아니면 지금 장바구니에 있는 것만 결제할까요?")
                        decision = {**decision, "verdict": "ask_user", "question": question}
                    # 최종 답 경로와 같은 가드: 이번 요청에서 이미 반영된 상태 변경이 있으면
                    # 자동 보완이 그 위에 또 다른 변경(다른 상품 담기 등)을 얹을 수 있다. 묻는다.
                    changed = [entry for entry in trace
                               if not entry.get("internal")
                               and entry.get("tool") in STATE_CHANGING
                               and entry.get("tool") != holding["tool"]
                               and (entry.get("result") or {}).get("success")]
                    if decision and decision["verdict"] == "retry" and changed:
                        question = ("일부 작업은 이미 반영되었고, 아직 처리하지 못한 것이 있습니다: "
                                    + "; ".join(decision["missing_requirements"])
                                    + "\n어떻게 할지 알려주세요.")
                        decision = {**decision, "verdict": "ask_user", "question": question}
                    if decision and decision["verdict"] == "retry":
                        verifier_retries += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                "[결과 검증] 확인 버튼을 띄우기 전에 아직 충족되지 않은 요구가 있습니다: "
                                + "; ".join(decision["missing_requirements"])
                                + "\n다음 지시만 안전하게 보완하세요: "
                                + decision["next_instruction"]
                                + "\n확인 대기는 열리지 않았습니다. 보완이 끝나면 원래 하려던 작업"
                                  f"({holding['tool']})을 다시 부르세요. "
                                  "최종 답에는 원래 요청의 모든 부탁(조회해서 알려달라고 한 목록 포함)을 담으세요. "
                                  "이미 성공한 상태 변경 Tool은 다시 실행하지 말고, "
                                  "사용자가 명시한 조건을 임의로 완화하지 마세요."
                            ),
                        })
                        continue
                    if decision and decision["verdict"] == "ask_user":
                        prefix = self._result_report(trace, always=True).strip()
                        prefix = (prefix + "\n\n") if prefix else ""
                        return prefix + decision["question"], trace
                # 줄바꿈 하나는 마크다운에서 합쳐진다. 항목이 여럿이면 한 문단으로
                # 붙어 버려서 몇 건인지 보이지 않는다.
                summary = "\n\n".join(item["label"] for item in items)
                summary += ("\n\n아래 버튼으로 선택해 주세요. "
                            "되돌릴 수 없는 작업이라 채팅 답변으로는 진행되지 않습니다.")
                self.pending = PendingAction(
                    tool=holding["tool"], items=items, summary=summary, turn=self.turn,
                    from_request=bool(self.last_request
                                      and self.last_request["turn"] == self.turn))
                if self.last_request and self.last_request["turn"] == self.turn:
                    # 이 요청이 승인 버튼으로 끊겼다. 승인 뒤 continue_after_approval 이 남은 부탁을 잇는다.
                    # 단, 이 턴에 부른 Tool 이 확인 대상 하나뿐이고("결제해줘", "어반 러너 빼줘")
                    # 요청 문장에 이어지는 말("~하고", "그리고", "대신")도 없으면 남은 부탁이
                    # 없다고 보고 이어가지 않는다. 승인마다 모델을 한 번 더 부르는 비용을 아낀다.
                    lookups = {"search_order", "search_product", "get_order", "cancel_possible",
                               "return_possible", "view_cart", "manage_preferences"}
                    others = [entry for entry in trace
                              if entry.get("tool") != holding["tool"] and not entry.get("internal")
                              and entry.get("tool") not in lookups]
                    connective = _has_connective(self.last_request["text"])
                    self.last_request["interrupted"] = bool(others) or connective

                # 같은 턴에 조회한 것(재고·주문 상태)이 있으면 확인 질문보다 먼저 답한다.
                # 그렇지 않으면 그 답은 어디에도 나타나지 않는다.
                spoken = self._answer_before_confirm(messages, trace, message.get("content"))
                spoken = (spoken + "\n\n") if spoken else ""

                # 확인을 묻기 전에, 이번 턴에 이미 실행된 것이 있으면 먼저 알린다.
                #
                # always=True 인 이유: 평소에는 성공 한 건이면 보고를 생략하지만
                # 여기서는 생략하면 안 된다. "B 담고 A 빼줘" 에서 B 는 이미 담겼는데
                # 화면에는 A 삭제 확인만 뜬다. 그 상태로 사용자가 거절하면
                # 아무 일도 없었다고 오해한다.
                prefix = self._result_report(trace, always=True).strip()
                prefix = spoken + ((prefix + "\n\n") if prefix else "")

                if postponed:
                    # 미룬 작업을 기억해 둔다. 안 그러면 사용자가 함께 부탁한 일이
                    # 조용히 사라진다. ("2개 빼고 주문까지" 에서 주문이 증발했다)
                    self.postponed = {"items": postponed, "turn": self.turn}
                    names = ", ".join(item["tool"] for item in postponed)
                    return (prefix + f"{summary}\n"
                            f"(먼저 이것부터 확인할게요. 승인해 주시면 이어서 "
                            f"{len(postponed)}건을 더 진행합니다: {names})"), trace
                # 새 확인 대기가 앞 확인을 덮었다. 앞 확인에 딸린 미룬 작업이 남으면
                # 이번 승인 뒤에 관계없는 작업이 이어서 열린다.
                self.postponed = None
                return prefix + summary, trace

            # --- ⑥ 다시 ① 로. 모델이 결과를 보고 다음 행동을 정한다 ---

        # 루프를 다 돌았는데도 안 끝난 경우.
        # 모델이 같은 Tool 을 계속 부르는 상황이 실제로 자주 발생한다.
        return (
            "요청을 처리하는 데 시간이 너무 오래 걸립니다. "
            "조건을 조금 더 구체적으로 알려주시겠어요?" + self._result_report(trace, interrupted=True),
            trace,
        )
