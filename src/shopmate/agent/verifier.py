"""계획 없이도 결과를 관찰하고 수정하는 에이전트 보조 계층.

이 모듈은 세 가지를 맡는다.

1. 사용자가 명시적으로 기억해 달라고 한 쇼핑 선호를 구조화해 보관한다.
2. Tool 실행 뒤 초안이 원래 요청을 충족했는지 별도 모델 호출로 검증한다.
3. 검증 결과를 complete / retry / ask_user 셋 중 하나로 제한한다.

실제 Tool 실행과 승인 정책은 여전히 agent.py가 소유한다. 검증 모델은 행동을
직접 실행할 수 없고, 다음 행동을 제안할 뿐이다.
"""

from __future__ import annotations

import json
import time


PREFERENCE_KEYS = {
    "gender": "주로 쇼핑하는 성별",
    "shoe_size": "신발 사이즈",
    "top_size": "상의 사이즈",
    "bottom_size": "하의 사이즈",
    "preferred_colors": "선호 색상",
    "preferred_materials": "선호 소재",
    "avoided_materials": "피하는 소재",
    "preferred_styles": "선호 스타일·핏",
    "usual_budget": "평소 예산",
}

SCALAR_KEYS = {"gender", "shoe_size", "top_size", "bottom_size", "usual_budget"}


MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "manage_preferences",
        "description": (
            "사용자가 '기억해줘', '앞으로', '평소 나는'처럼 지속적인 쇼핑 선호를 "
            "명시했거나 기존 선호를 지워 달라고 했을 때만 사용한다. 이번 검색에서만 "
            "말한 색상·가격·소재·사이즈는 저장하지 않는다. 이 호출은 상품이나 주문을 "
            "변경하지 않고 현재 사용자의 선호 메모리만 갱신한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "remember": {
                    "type": "array",
                    "maxItems": 10,
                    "description": "새로 저장하거나 바꿀 선호. 값은 사용자가 직접 말한 것만 쓴다.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "enum": list(PREFERENCE_KEYS)},
                            "values": {
                                "type": "array", "minItems": 1, "maxItems": 5,
                                "items": {"type": "string"},
                                "description": "정규화하지 않은 사용자의 명시적 선호 값",
                            },
                        },
                        "required": ["key", "values"],
                    },
                },
                "forget": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {"type": "string", "enum": list(PREFERENCE_KEYS)},
                    "description": "사용자가 지워 달라고 한 선호 항목",
                },
            },
        },
    },
}


MEMORY_PROMPT = """
사용자 선호 메모리:
- 사용자가 명시적으로 기억을 요청하거나 반복적으로 적용할 취향이라고 말한 경우에만
  manage_preferences를 호출하세요.
- "오늘 검은 재킷 찾아줘" 같은 이번 요청의 조건은 저장하지 않습니다.
- 저장된 선호는 사용자가 이번 요청에서 다른 조건을 말하지 않았을 때 참고합니다.
- 사이즈·성별처럼 명확한 값은 검색 인자에 쓸 수 있지만, 색상·스타일 취향은 사용자가
  필수라고 하지 않았다면 상품을 탈락시키는 강제 필터로 바꾸지 마세요.
- 사용자가 저장된 선호와 다른 조건을 말하면 현재 요청이 우선입니다.
- 사용자가 무엇을 기억하고 있는지 물으면 [현재 상태]의 선호 메모리만 답하세요.
"""


ADAPTIVE_PROMPT = """
관찰과 복구:
- Tool 결과를 받은 뒤 원래 요청의 조건과 아직 하지 않은 일을 다시 확인하세요.
- Tool 실패 원인을 읽고, 같은 인자로 같은 실패를 반복하지 마세요.
- 오타·표준값 변환처럼 사용자의 뜻이 바뀌지 않는 수정은 스스로 고쳐 다시 호출할 수 있습니다.
- 사용자가 명시한 카테고리·성별·색상·소재·사이즈·가격 조건을 허락 없이 제거하거나
  다른 값으로 바꾸지 마세요. 조건 완화나 대상 선택이 필요하면 사용자에게 물으세요.
- 이전 요청을 가리키는 "그중", "아까", "두 번째", "더 저렴한", "색상만 바꿔서"가
  나오면 [현재 상태]의 직전 검색 조건과 실제 결과 ID를 사용하세요. 언급되지 않은 기존
  조건은 유지하고, 사용자가 바꾼 조건만 교체하세요.
"""


VERIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "verify_outcome",
        "description": (
            "원래 사용자 요청, 실제 Tool 실행 기록, 현재 상태와 답변 초안을 대조한다. "
            "직접 Tool을 실행하지 않으며 완료 여부와 다음 행동만 구조화해서 반환한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["complete", "retry", "ask_user"],
                    "description": (
                        "complete는 요청 충족, retry는 사용자 선택 없이 안전하게 보완 가능, "
                        "ask_user는 조건 완화·대상 선택 등 새 판단이 필요한 경우"
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "판정 근거를 한 문장으로. 숨은 사고과정이 아니라 확인된 누락만 쓴다.",
                },
                "missing_requirements": {
                    "type": "array", "maxItems": 5,
                    "items": {"type": "string"},
                    # 재시도 한도를 넘으면 이 값이 사용자 답변에 그대로 붙는다.
                    "description": ("아직 충족되지 않은 사용자 요구. 사용자에게 그대로 보여 줄 수 있는 "
                                    "짧은 조건 이름으로(예: '봄 착용', '예산'). Tool 이름·내부 용어 금지"),
                },
                "next_instruction": {
                    "type": "string",
                    "description": "retry일 때 실행 모델에게 줄 구체적 지시. 해당 없으면 빈 문자열.",
                },
                "question": {
                    "type": "string",
                    "description": "ask_user일 때 사용자에게 물을 한 가지 질문. 해당 없으면 빈 문자열.",
                },
            },
            "required": ["verdict", "summary", "missing_requirements",
                         "next_instruction", "question"],
        },
    },
}


VERIFY_PROMPT = """당신은 쇼핑 에이전트의 결과 검증기입니다.
반드시 verify_outcome 하나만 호출하세요. 상품이나 주문 Tool은 사용할 수 없습니다.

판정 규칙:
- 모델의 답변 문장이 아니라 실제 Tool 기록과 현재 상태를 근거로 판단합니다.
- 사용자 조건은 original_request 한 문장이 아니라 recent_user_turns(이전 턴 포함, 최근 것이 먼저)
  전체와 current_state 의 저장된 선호입니다. "상의", "색은 상관없어" 같은 짧은 말은 앱의 되묻기에
  대한 답이라 앞 턴 요청을 이어서 완성합니다. 앞 턴에서 말한 성별·가격·종류를 반영한 것은
  임의로 넣은 조건이 아닙니다.
- recent_user_turns 의 각 줄 앞 [이번 턴]·[n턴 전]이 순서입니다. 같은 조건을 다르게 말했으면
  ("남성용" 뒤에 "여성용") 더 최근 말이 우선입니다. 목록은 가장 최근에 사진을 올린 턴에서
  끝나므로, 목록에 없는 이전 대화의 조건을 다시 넣으라고 지시하지 마세요.
- 사용자의 명시적 조건과 요청한 작업을 모두 충족했으면 complete입니다.
- 누락된 조회나 비교처럼 사용자 선택 없이 안전하게 보완 가능하면 retry입니다.
- 조건 완화, 여러 대상 중 선택, 새로운 사이즈·예산 결정이 필요하면 ask_user입니다.
- 성공한 장바구니 변경·결제·취소·반품을 다시 실행하라고 지시하지 않습니다.
- 검색 결과가 없다고 사용자의 필터를 제거하지 않습니다.
- 단순한 표현 차이와 답변 문체는 실패로 보지 않습니다.
- draft_answer 가 "담았다·결제했다·취소했다·반품 신청했다"고 말한 작업은 tool_evidence 에 성공한
  실행 기록이 있고 current_state 에도 반영돼 있어야 합니다. 없으면 retry 로 그 작업을 지시합니다.
- "하나씩", "두 개"처럼 개수를 정한 요청은 current_state 장바구니의 해당 품목 개수가 정확히 맞아야
  complete 입니다. 요청과 다른 상품(예: 반팔 요청에 민소매)이 남았거나 개수가 넘치면 무엇을 빼거나
  바꿀지 missing_requirements 에 적습니다.
- 검색 기록 읽는 법:
  - ranking=sql_fallback 은 의미 검색을 못 써 구조화 조건만으로 찾은 결과입니다. 답변이 이를
    감추지 않았으면 실패로 보지 않습니다.
  - backfilled>0 은 딱 맞는 상품이 적어 비슷한 상품을 함께 보여준 것입니다. 답변이 이를
    알렸으면 complete 입니다.
  - 사진 검색의 hard_filters 는 실제로 적용한 조건, unapplied_soft_filters 는 사용자가 말하지 않아
    적용하지 않은 사진 추정값입니다. 후자를 누락으로 보지 마세요.
  - relative_applied=false 는 상대 조건("더 밝은")을 순위에 반영하지 못했다는 뜻입니다. 답변이
    미적용 사실을 알렸다면 complete이며 comparing_info로 후보를 하나씩 확인하라고 retry하지 마세요.
  - 사진 검색은 사진 속 아이템 하나로 찾습니다. other_items 는 "다른 것도 있다"는 안내일 뿐 할 일이
    아닙니다. 사용자가 그 아이템을 말하지 않았으면 추가 검색을 retry 하거나 ask_user 하지 마세요.
"""


VERIFY_BEFORE_CONFIRM_PROMPT = """
지금 단계: 되돌릴 수 없는 작업(about_to_confirm)의 확인 버튼을 사용자에게 띄우기 직전입니다.
- about_to_confirm 에 적힌 작업 자체는 아직 실행되지 않은 것이 정상입니다. 그것을 누락으로 보지 마세요.
- 그 작업을 승인받기 **전에** 끝나 있어야 할 요구가 빠졌는지만 봅니다.
  예: "둘 다 결제" 인데 담기 하나가 재고 부족으로 실패해 장바구니에 하나만 있다.
- 빠진 것이 같은 조건의 다음 후보로 대신 담기처럼 사용자 선택 없이 보완 가능하면 retry 로,
  next_instruction 에 "확인 대기는 아직 열리지 않았으니 보완한 뒤 원래 작업을 다시 부르세요" 를 포함해 지시하세요.
- 조건 완화나 대상 선택이 필요하면 ask_user 로 한 가지만 물으세요.
- 빠진 것이 없으면 complete 입니다.
"""


class PreferenceMemory:
    """사용자가 명시한 선호만 저장하는 작은 구조화 메모리."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})

    def update(self, arguments):
        if not isinstance(arguments, dict):
            return _fail("선호 메모리 인자는 객체여야 합니다.")

        remember = arguments.get("remember") or []
        forget = arguments.get("forget") or []
        if not isinstance(remember, list) or not isinstance(forget, list):
            return _fail("remember와 forget은 배열이어야 합니다.")
        if not remember and not forget:
            return _fail("저장하거나 지울 선호가 없습니다.")

        # 전부 검사한 뒤에 한꺼번에 반영한다. 두 번째 항목이 틀렸다고 실패를 돌려주면서
        # 첫 번째는 이미 저장돼 있으면, 모델은 "저장 안 됨" 으로 읽고 사용자에게 그렇게 말한다.
        from shopmate.agent.tools import normalize_size

        updates = {}
        for item in remember[:10]:
            if not isinstance(item, dict):
                return _fail("remember의 각 항목은 객체여야 합니다.")
            key = item.get("key")
            values = item.get("values")
            if key not in PREFERENCE_KEYS:
                return _fail(f"저장할 수 없는 선호 항목입니다: {key}")
            if not isinstance(values, list):
                return _fail(f"{key}의 values는 배열이어야 합니다.")
            cleaned = []
            for value in values:
                text = str(value).strip()
                if text and key.endswith("_size"):
                    # 검색·담기와 같은 표기("m" -> "M", "XXL" -> "2XL")로 둬야 그대로 쓸 수 있다.
                    try:
                        text = normalize_size(text)
                    except ValueError as error:
                        return _fail(f"{key}: {error}")
                if text and text[:80] not in cleaned:
                    cleaned.append(text[:80])
            if not cleaned:
                return _fail(f"{key}에 저장할 값이 없습니다.")
            updates[key] = cleaned[:1] if key in SCALAR_KEYS else cleaned[:5]
        for key in forget[:10]:
            if key not in PREFERENCE_KEYS:
                return _fail(f"지울 수 없는 선호 항목입니다: {key}")

        changed = []
        for key, cleaned in updates.items():
            self.entries[key] = {"values": cleaned, "updated_at": time.time()}
            changed.append(f"{PREFERENCE_KEYS[key]}={', '.join(cleaned)}")
        removed = []
        for key in forget[:10]:
            if key in self.entries:
                self.entries.pop(key, None)
                removed.append(PREFERENCE_KEYS[key])

        pieces = []
        if changed:
            pieces.append("기억함: " + " / ".join(changed))
        if removed:
            pieces.append("삭제함: " + ", ".join(removed))
        if not pieces:
            pieces.append("변경된 선호가 없습니다.")
        return {
            "success": True,
            "data": {"preferences": self.public()},
            "message": "사용자 선호 메모리 — " + " · ".join(pieces),
        }

    def public(self):
        return {key: list(value.get("values") or [])
                for key, value in self.entries.items() if key in PREFERENCE_KEYS}

    def state_line(self):
        values = self.public()
        if not values:
            return "저장된 사용자 선호 없음"
        return " / ".join(
            f"{PREFERENCE_KEYS[key]}: {', '.join(items)}"
            for key, items in values.items()
        )

    def to_dict(self):
        return {key: {"values": list(value.get("values") or []),
                      "updated_at": value.get("updated_at")}
                for key, value in self.entries.items() if key in PREFERENCE_KEYS}

    @classmethod
    def from_dict(cls, data):
        return cls(data if isinstance(data, dict) else {})


def _fail(message):
    return {"success": False, "data": None, "message": message}


def validate_verdict(arguments):
    """검증 모델 출력을 안전한 세 가지 상태로 제한한다."""
    if not isinstance(arguments, dict):
        return None
    verdict = arguments.get("verdict")
    if verdict not in {"complete", "retry", "ask_user"}:
        return None
    missing = arguments.get("missing_requirements") or []
    if not isinstance(missing, list):
        return None
    cleaned = {
        "verdict": verdict,
        "summary": str(arguments.get("summary") or "").strip()[:300],
        "missing_requirements": [str(value).strip()[:120] for value in missing[:5]
                                 if str(value).strip()],
        "next_instruction": _optional_text(arguments.get("next_instruction"), 500),
        "question": _optional_text(arguments.get("question"), 300),
    }
    if verdict == "retry" and not cleaned["next_instruction"]:
        return None
    if verdict == "ask_user" and not cleaned["question"]:
        return None
    return cleaned


def _optional_text(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def compact_trace(trace):
    """검증기에 필요한 사실만 남긴다. 검색 상품 50개를 다시 보내지 않는다."""
    rows = []
    for entry in trace or []:
        if entry.get("internal"):
            continue
        result = entry.get("result") or {}
        data = result.get("data") or {}
        fact = {
            "tool": entry.get("tool"),
            "arguments": entry.get("arguments") or {},
            "success": bool(result.get("success")),
            "status": result.get("status"),
            "message": str(result.get("message") or "")[:500],
        }
        if isinstance(data, dict):
            for key in ("total", "qualified", "shown", "displayed", "ranking", "backfilled",
                        "hard_filters", "unapplied_soft_filters", "relative_applied",
                        "requires_confirmation"):
                if key in data:
                    fact[key] = data[key]
            products = data.get("products")
            if isinstance(products, list):
                fact["product_ids"] = [
                    row.get("product_id") or row.get("id")
                    for row in products[:10] if isinstance(row, dict)
                ]
        rows.append(fact)
    return rows[-12:]


def verifier_messages(user_message, draft, trace, current_state, about_to_confirm=None,
                      recent_turns=None):
    """검증기 입력. about_to_confirm 이 있으면 '확인 버튼 직전' 단계의 검증이다.

    recent_turns 는 이전 턴을 포함한 사용자 원문(최근 것이 먼저, filter_rules.user_turns).
    이것 없이 이번 턴만 보면, 되묻기에 "상의" 라고 답한 턴에서 앞 턴의 "남자·10만원 이하" 가
    모델이 지어낸 조건으로 보여 검증기가 retry 를 두 번 냈다(2026-09-30 상담 기록).
    검색 Tool 의 필터 판정은 이미 같은 목록을 본다 — 검증기와 기준을 맞춘다.
    """
    evidence = {
        "original_request": user_message,
        "recent_user_turns": [
            f"[{'이번 턴' if index == 0 else f'{index}턴 전'}] {text}"
            for index, text in enumerate(list(recent_turns or [user_message])[:5])],
        "tool_evidence": compact_trace(trace),
        "current_state": current_state,
        "draft_answer": draft,
    }
    prompt = VERIFY_PROMPT
    if about_to_confirm:
        evidence["about_to_confirm"] = list(about_to_confirm)
        prompt = VERIFY_PROMPT + VERIFY_BEFORE_CONFIRM_PROMPT
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(evidence, ensure_ascii=False, default=str)},
    ]
