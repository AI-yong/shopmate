"""검색 조건 중 "사용자가 직접 말한 값"을 가려내는 단일 판정 지점.

사진 검색에서는 같은 축(종류·색·소재)의 값이 두 곳에서 올 수 있다.
- 사용자가 말한 것: "블랙으로" → 모델이 enum 표준값 color=검은색으로 바꿔 넣는다.
- 사진에서 VLM이 추정한 것: 검증 블록의 "니트 / 초록색"을 모델이 옮겨 넣는다.

앞의 것은 하드 필터(풀지 않음), 뒤의 것은 기준 필터(결과가 모자라면 푼다)다.
표준값 "검은색"은 원문에 글자 그대로 없을 수 있으므로, 모델이 근거가 된 원문 표현
(user_quotes: {"color": "블랙"})을 함께 넘기고 여기서 그 표현이 원문에 있는지만 확인한다.
번역(블랙→검은색)은 모델이, 확인(정말 말했나)은 이 모듈이 맡는다. 동의어 표는 두지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# 사진 추정값이 끼어들 수 있는 축. 가격·사이즈·재고·브랜드·성별은 사진에서 나오지 않는다.
REFERENCE_AXES = ("category", "material", "color")

# 표현 바로 뒤(공백 제거 후 몇 글자 안)에 이 말이 오면 "그 값은 원하지 않는다"로 본다.
# "빼"는 빼고·빼 줘·빼주세요를, "뺀"은 "검은색 뺀 나머지"를 함께 잡는다.
# "빼고"만 있을 때 "검은색은 빼 줘"가 긍정으로 판정돼 검은색 필터가 걸렸다.
NEGATIONS = ("말고", "말구", "빼", "뺀", "제외", "아닌", "아니고", "싫")
_NEGATION_WINDOW = 5

# 이전 턴에 말한 조건도 대화가 이어지는 동안은 사용자가 말한 것이다.
MAX_USER_TURNS = 5


@dataclass
class FilterResolution:
    hard: dict[str, Any] = field(default_factory=dict)      # 사용자가 말한 값 → SQL 하드 필터
    soft: dict[str, Any] = field(default_factory=dict)      # 근거 없음 → 기준 필터(완화 가능)
    dropped: dict[str, Any] = field(default_factory=dict)   # 사용자가 부정한 값 → 검색 결과에서 제외
    reasons: dict[str, str] = field(default_factory=dict)   # 축별 판정 이유(디버그·응답용)


def _is_hangul(char: str) -> bool:
    return "가" <= char <= "힣"


def _compact(text: str) -> str:
    return "".join(text.split())


def _find(text: str, phrase: str) -> int | None:
    """phrase가 끝나는 위치. 띄어쓰기가 달라도("검은 색"↔"검은색") 찾는다.

    한 글자 표현("면")은 "그러면"의 끝 글자에도 걸리므로, 띄어쓰기가 살아 있는 원문에서
    앞 글자가 한글이 아닐 때만 인정한다.
    """
    if len(_compact(phrase)) == 1:
        start = text.find(phrase)
        while start != -1:
            if start == 0 or not _is_hangul(text[start - 1]):
                return start + len(phrase)
            start = text.find(phrase, start + 1)
        return None
    compact_text, compact_phrase = _compact(text), _compact(phrase)
    start = compact_text.find(compact_phrase)
    if start == -1:
        return None
    # 공백을 뺀 위치를 원문 위치로 되돌린다.
    seen = 0
    for index, char in enumerate(text):
        if not char.isspace():
            seen += 1
        if seen == start + len(compact_phrase):
            return index + 1
    return None


def _mention(texts: list[str], phrase: str) -> str | None:
    """가장 최근 원문부터 보고 phrase가 "stated"인지 "negated"인지, 없으면 None."""
    for text in texts:
        end = _find(text, phrase)
        if end is None:
            continue   # 이 턴에서 못 찾으면 이전 턴으로 간다.
        # user_quotes는 값만("검은색") 올 수도 있고 부정 표현까지 포함한
        # 근거 구절("검은색 말고")로 올 수도 있다. 후자의 경우 phrase 뒤만 보면
        # 말고가 이미 phrase 안에 들어가 있어 긍정으로 오판하므로 함께 검사한다.
        if any(word in _compact(phrase) for word in NEGATIONS):
            return "negated"
        tail = _compact(text[end:])[:_NEGATION_WINDOW]
        return "negated" if any(word in tail for word in NEGATIONS) else "stated"
    return None


def _candidates(value: Any, quote: str | None) -> list[str]:
    """원문에서 찾아볼 표현. 모델이 준 인용이 먼저, 그다음 표준값 자체."""
    phrases = []
    if quote:
        phrases.append(" ".join(str(quote).split()))
    raw = " ".join(str(value).split())
    phrases.append(raw)
    if raw.endswith("색") and len(raw) > 2:   # "검은색"↔"검은", "초록색"↔"초록"
        phrases.append(raw[:-1])
    return [phrase for phrase in dict.fromkeys(phrases) if phrase]


def user_turns(current: str | None, history: Iterable[dict] | None = None,
               marker: str = "\n\n[검증된 첨부 이미지]") -> list[str]:
    """판정에 쓸 사용자 원문 목록(최근 것이 먼저). 서버가 붙인 검증 블록과 앱 메시지는 뺀다.

    가장 최근에 사진을 올린 턴까지만 센다. 새 사진은 새 검색이다 — 앞 사진에서 말한
    "남성용", "초록색으로 해줘" 가 두 번째 사진의 하드 필터가 되고, 검증기가 그 "남성용" 을
    최신 요청으로 읽어 재검색을 지시했다(2026-09-30 상담 기록).
    """
    texts = []
    candidates = [current] + [
        message.get("content") for message in reversed(list(history or []))
        if isinstance(message, dict) and message.get("role") == "user"]
    for content in candidates:
        if not isinstance(content, str) or content.startswith("(앱) "):
            continue
        text = content.split(marker, 1)[0].strip()
        if text:
            texts.append(text)
        if marker in content or len(texts) >= MAX_USER_TURNS:
            break
    return texts


def resolve(arguments: dict[str, Any], *, user_texts: list[str] | None,
            quotes: dict[str, str] | None = None) -> FilterResolution:
    """category/material/color를 hard·soft·dropped로 나눈다. 나머지 인자는 그대로 hard.

    user_texts가 비어 있으면 원문을 모르므로 판정하지 않고 전부 hard다.
    """
    result = FilterResolution()
    texts = [" ".join(text.split()) for text in (user_texts or []) if text and text.strip()]
    quotes = quotes or {}
    for key, value in arguments.items():
        if value is None:
            continue
        if key not in REFERENCE_AXES or not texts:
            result.hard[key] = value
            continue
        verdict, phrase = None, None
        for phrase in _candidates(value, quotes.get(key)):
            verdict = _mention(texts, phrase)
            if verdict:
                break
        if verdict == "stated":
            result.hard[key] = value
            result.reasons[key] = f"사용자 표현 '{phrase}' 확인"
        elif verdict == "negated":
            result.dropped[key] = value
            result.reasons[key] = f"사용자가 '{phrase}'을(를) 원하지 않음"
        else:
            result.soft[key] = value
            result.reasons[key] = "사용자 원문에 근거 없음(사진 추정값일 수 있음)"
    return result
