"""설정 한 곳 모음.

서버 주소, 모델 이름, API 키를 .env 파일에서 읽습니다.
코드 어디에도 키를 하드코딩하지 않기 위한 파일입니다.

.env 는 .gitignore 에 등록되어 있어 절대 커밋되지 않습니다.
새로 클론한 사람은 .env.example 을 복사해서 값을 채우면 됩니다.

    cp .env.example .env
"""

import json
import os
from pathlib import Path


# 저장소 루트. .env 와 web/ 이 여기 있다. 소스에서 실행(pip install -e .)하면 src/shopmate 의
# 두 단계 위이고, 일반 설치로 site-packages 에서 돌면 실행한 폴더를 루트로 본다.
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = _SOURCE_ROOT if (_SOURCE_ROOT / "pyproject.toml").exists() else Path.cwd()
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """.env 파일을 읽어 os.environ 에 채운다.

    python-dotenv 를 쓰지 않고 직접 파싱합니다. 의존성을 늘리지 않기 위해서이며,
    형식은 KEY=VALUE 한 줄에 하나, # 로 시작하면 주석입니다.

    이미 환경변수로 설정된 값은 덮어쓰지 않습니다.
    (터미널에서 export 한 값이 .env 보다 우선한다는 뜻)
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")  # 따옴표로 감싼 값도 허용

        if key and key not in os.environ:
            os.environ[key] = value


# 모델별 설정 파일(선택). 먼저 읽어서 .env 의 같은 키보다 우선한다.
#     ENV_FILE=my-model.env python server.py
MODEL_ENV_FILE = os.environ.get("ENV_FILE", "")
if MODEL_ENV_FILE:
    _model_env_path = Path(MODEL_ENV_FILE)
    if not _model_env_path.is_absolute():
        _model_env_path = PROJECT_ROOT / _model_env_path
    if not _model_env_path.exists():
        raise FileNotFoundError(f"ENV_FILE 을 찾을 수 없습니다: {_model_env_path}")
    _load_env_file(_model_env_path)

_load_env_file(ENV_PATH)


# --- 데이터베이스 ----------------------------------------------------------
# 업무 데이터(상품·재고·주문·선호)와 세션 상태(승인 대기·대화 문맥)를 **다른 DB** 에
# 둡니다. 수명주기가 다르기 때문입니다 — 주문은 영구·백업 대상이고, 세션은
# 매 요청 쓰이고 분~시간 뒤 버려집니다.
# 기본값은 지금 쓰는 구성 — **인스턴스 하나(5432)에 DB 둘(shop · session)** 입니다.
# deploy/docker-compose.yml 은 세션 DB 를 따로 띄우는 예시(5433)라, 그걸 쓰면 포트만 바꿉니다.
SHOP_DSN = os.environ.get("SHOP_DSN", "postgresql://postgres@127.0.0.1:5432/shop")
SESSION_DSN = os.environ.get("SESSION_DSN",
                             "postgresql://postgres@127.0.0.1:5432/session")
# 웹 서버(server.py)만 쓰는 최소 권한 접속 (deploy/roles.sql 의 app_shop · app_session).
# 테이블을 만들거나 바꾸지 못한다. 비어 있으면 위의 소유자 접속을 그대로 쓴다 —
# 임베딩·승격 같은 오프라인 배치는 스키마를 만져야 하므로 계속 SHOP_DSN 을 쓴다.
APP_SHOP_DSN = os.environ.get("APP_SHOP_DSN") or SHOP_DSN
APP_SESSION_DSN = os.environ.get("APP_SESSION_DSN") or SESSION_DSN

# 세션 쿠키. 값은 랜덤 토큰이고 DB 에는 해시만 있다 (db.create_web_session).
SESSION_MAX_AGE_SECONDS = int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(30 * 24 * 3600)))
# HTTPS 로 서비스하면 1. 지금처럼 http 로 LAN 에서 열면 0 이어야 브라우저가 쿠키를 보낸다.
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0").strip().lower() in (
    "1", "true", "yes")
# 시연용 데모 사용자. 화면 헤더에서 골라 그 사용자로 전환한다 (server.api_switch_user).
# 비밀번호가 없는 **데모용 사용자 선택**이지 로그인이 아니다. 이 목록에 있는 id 로만 전환된다.
DEMO_USERS = tuple(u.strip() for u in os.environ.get(
    "DEMO_USERS", "info1,info2,info3,info4,info5").split(",") if u.strip())
# 대화 문맥·직전 결과(세션 DB agent_state)를 마지막 사용 뒤 몇 시간 두는가.
SESSION_STATE_IDLE_HOURS = int(os.environ.get("SESSION_STATE_IDLE_HOURS", "24"))
# 만료된 승인·대화 상태·사진 질의·세션을 몇 초마다 치우는가 (server.start_janitor).
PURGE_INTERVAL_SECONDS = int(os.environ.get("PURGE_INTERVAL_SECONDS", "600"))


# --- 이미지 질의 -----------------------------------------------------------
# 카탈로그 이미지와 사용자 질의 이미지를 같은 MinIO에 두되 prefix로 분리한다.
# 원본 업로드는 저장하지 않고 EXIF 제거·크기 제한을 거친 JPEG만 임시 보관한다.
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "fashion-catalog")
MINIO_SECURE = os.environ.get("MINIO_SECURE", "0").strip().lower() in (
    "1", "true", "yes", "on")
IMAGE_QUERY_MAX_BYTES = int(os.environ.get("IMAGE_QUERY_MAX_BYTES", str(8 * 1024 * 1024)))
IMAGE_QUERY_MAX_PIXELS = int(os.environ.get("IMAGE_QUERY_MAX_PIXELS", "25000000"))
IMAGE_QUERY_TTL_SECONDS = int(os.environ.get("IMAGE_QUERY_TTL_SECONDS", "3600"))
if min(IMAGE_QUERY_MAX_BYTES, IMAGE_QUERY_MAX_PIXELS, IMAGE_QUERY_TTL_SECONDS) <= 0:
    raise ValueError("이미지 질의 용량·픽셀·TTL 설정은 1 이상이어야 합니다.")


# --- 임베딩 ----------------------------------------------------------------
# 문서와 질의를 같은 모델로 만들어야 합니다. 다르면 조용히 엉뚱한 결과가 나옵니다.
# 그래서 굽고 나면 meta.embed_model / embed_dim 에 적고, DB 열 차원과 다르면 멈춥니다.
#
# nlpai-lab/KURE-v1 : bge-m3 기반 한국어 검색 전용, 1024차원, 최대 8192토큰.
EMBED_MODEL_NAME = os.environ.get("EMBED_MODEL_NAME", "nlpai-lab/KURE-v1")

# 사진 검색 백엔드.
#   qwen : Qwen3-VL 상주 임베딩 서비스 + 상품 문서 벡터. 기본값
#   rrf  : SigLIP + KURE 순위 결합(RRF). qwen이 실패하면 항상 이 경로로 폴백한다
MULTIMODAL_BACKEND = os.environ.get("MULTIMODAL_BACKEND", "qwen").strip().lower() or "qwen"
if MULTIMODAL_BACKEND not in ("qwen", "rrf"):
    raise ValueError(
        f"MULTIMODAL_BACKEND는 qwen·rrf 중 하나여야 합니다: {MULTIMODAL_BACKEND}")
# 폴백 사진 검색용 다국어 SigLIP 2. 검증한 commit 을 고정해 모델이 조용히 바뀌지 않게 한다.
SIGLIP_MODEL_NAME = os.environ.get("SIGLIP_MODEL_NAME", "google/siglip2-base-patch16-384")
SIGLIP_MODEL_REVISION = os.environ.get(
    "SIGLIP_MODEL_REVISION", "f775b65a79762255128c981547af89addcfe0f88").strip()
# Qwen 상품 문서 벡터의 조리법. image_search_text = 사진 + search_text(속성 문장 포함).
# 오프라인 비교에서 image_text보다 같거나 나았다. 이 저장소에는 상품 벡터 생성 스크립트가 없으므로
# 어느 조리법이든 그 조리법의 벡터를 product_multimodal_embeddings 에 직접 적재해야 한다.
QWEN3_VL_RECIPE = os.environ.get("QWEN3_VL_RECIPE", "image_search_text").strip()
if QWEN3_VL_RECIPE not in ("image_text", "image_search_text"):
    raise ValueError(
        f"QWEN3_VL_RECIPE는 image_text·image_search_text 중 하나여야 합니다: {QWEN3_VL_RECIPE}")


# --- 로컬 모델 서버 설정 ---------------------------------------------------

LOCAL_API_BASE_URL = os.environ.get("LOCAL_API_BASE_URL", "http://127.0.0.1:4000/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "gemma-4-31b")
SHOPPING_VLM_MODEL = os.environ.get("SHOPPING_VLM_MODEL") or MODEL_NAME
LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY", "")

# 모델 호출 기본값
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.2"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1024"))

# 에이전트가 한 번의 사용자 요청에 대해 Tool 을 연달아 호출할 수 있는 최대 횟수.
# 무한 루프(같은 Tool 을 계속 부르는 상황)를 막는 안전장치입니다.
# 8 이었을 때 "예산 안에서 4종 코디" 처럼 검색·담기가 4번씩 이어지는 요청(8~9왕복)이
# 마지막 한 걸음 앞에서 끊겼다. 같은 호출 반복은 이제 agent 가 따로 막으므로 여유를 둔다.
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "12"))

# 의미 검색 결과에 적용할 코사인 유사도 하한입니다. 모델이 Tool 인자로 정하는
# 값이 아니라 검색 서비스 전체에 동일하게 적용되는 정책입니다.
# 현재 0.40은 실제 질의 표본의 점수 분포로 정한 임시값이며, 사람 평가 후 조정합니다.
SEMANTIC_MIN_SCORE = float(os.environ.get("SEMANTIC_MIN_SCORE", "0.40"))
if not -1.0 <= SEMANTIC_MIN_SCORE <= 1.0:
    raise ValueError("SEMANTIC_MIN_SCORE는 -1.0 이상 1.0 이하여야 합니다.")

# 추천순(베이지안 평균)의 사전 리뷰 수 m. 리뷰가 m개보다 적은 상품의 평점은
# 카탈로그 전체 평균 쪽으로 끌려갑니다. 리뷰 6개 평점 2.1 같은 상품이 위로
# 오지 않게 하는 장치입니다. 10은 카탈로그 리뷰 수 분포의 약 80퍼센타일
# (중앙값 3, 90퍼센타일 19)로 정한 값입니다.
RECOMMEND_PRIOR_REVIEWS = int(os.environ.get("RECOMMEND_PRIOR_REVIEWS", "10"))
if RECOMMEND_PRIOR_REVIEWS < 1:
    raise ValueError("RECOMMEND_PRIOR_REVIEWS는 1 이상이어야 합니다.")

# 확인 대기(승인 버튼)가 유효한 시간(초). 지나면 버튼을 눌러도 실행되지 않고
# 다시 확인받습니다. HTTP 에는 "턴" 이 없어서 — 사용자가 아무 말 없이 10분 뒤
# 버튼을 누를 수 있어서 — 턴 수와 별개로 시간으로도 만료시킵니다.
PENDING_TTL_SECONDS = int(os.environ.get("PENDING_TTL_SECONDS", "300"))
if PENDING_TTL_SECONDS <= 0:
    raise ValueError("PENDING_TTL_SECONDS는 1초 이상이어야 합니다.")

# Tool 실행이 끝난 뒤 별도 검증 모델이 원래 요청과 실제 결과를 대조합니다.
# 부족한 조회처럼 안전하게 보완 가능한 경우에만 제한된 횟수로 다시 시도합니다.
# 승인 뒤 이어가기(continue_after_approval)의 실행 흐름은 바꾸지 않습니다.
ADAPTIVE_AGENT_MODE = os.environ.get("ADAPTIVE_AGENT_MODE", "1").strip().lower() not in (
    "0", "false", "no", "off")
MAX_VERIFIER_RETRIES = int(os.environ.get("MAX_VERIFIER_RETRIES", "1"))
# 되돌릴 수 없는 작업의 확인 버튼을 띄우기 **직전**에도 검증기를 돌립니다.
# 최종 답 직전에만 돌리면 확인 대기로 끝나는 턴(취소·반품·결제)은 검증을 건너뛰게 되어,
# "둘 다 결제" 인데 담기 하나가 재고로 실패한 채 결제 확인이 뜨는 경우를 잡지 못했습니다.
# 비용을 아끼기 위해 이번 턴에 실패한 Tool 이 있을 때만 돕니다. ADAPTIVE_AGENT_MODE 가 켜져 있어야 합니다.
VERIFY_BEFORE_CONFIRM = os.environ.get("VERIFY_BEFORE_CONFIRM", "1").strip().lower() not in (
    "0", "false", "no", "off")
if not 0 <= MAX_VERIFIER_RETRIES <= 2:
    raise ValueError("MAX_VERIFIER_RETRIES는 0 이상 2 이하여야 합니다.")

# 사용자가 명시적으로 '기억해 달라'고 한 장기 쇼핑 선호만 세션 상태에 저장합니다.
USER_MEMORY_ENABLED = os.environ.get("USER_MEMORY_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")

# 승인 버튼으로 끊긴 요청의 남은 부분을 승인 직후 이어갑니다(계획 없이도).
# "취소하고 흰 운동화 담아줘" 에서 승인 뒤 담기가 진행되지 않던 문제. 승인 한 번당 모델 호출 +1.
RESUME_AFTER_APPROVAL = os.environ.get("RESUME_AFTER_APPROVAL", "1").strip().lower() not in (
    "0", "false", "no", "off")

# 의미 유사도 기준(SEMANTIC_MIN_SCORE)을 통과한 상품이 이 수보다 적으면, 기준 아래에서
# 점수 순으로 채워 최소 이만큼은 보여준다. "크롭" 처럼 짧은 질의는 임베딩 점수가 전체적으로
# 낮아 정답 5개가 전부 기준 아래로 떨어져 0개가 나오는 일이 있었다.
# 그때 결과가 적다는 것은 "상품이 없다" 가 아니라 "이 질의엔 기준이 안 맞는다" 는 신호다.
# 6 = 화면 그리드 한 줄. 통과가 이보다 많으면 아무 일도 하지 않는다.
SEMANTIC_MIN_RESULTS = int(os.environ.get("SEMANTIC_MIN_RESULTS", "6"))
if SEMANTIC_MIN_RESULTS < 0:
    raise ValueError("SEMANTIC_MIN_RESULTS는 0 이상이어야 합니다.")


def _load_extra_body():
    """서버별 추가 파라미터를 읽는다.

    OpenAI 표준에 없는 옵션을 요구하는 서버가 있습니다.
    OpenAI 호환 서버 뒤의 Gemma 4 31B는 thinking 모드가 기본 켜져 있어
    끄지 않으면 답이 길어지고 max_tokens 를 사고에 다 씁니다.
    enable_thinking 은 Gemma 4 공식 챗 템플릿의 변수입니다 (Qwen3 도 같은 이름을 씁니다).
    thinking 을 켜서 쓰려면 _assistant_message 가 reasoning_content 도 되돌려 보내야 합니다.

        EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}

    이런 걸 코드에 하드코딩하면 서버를 바꿀 때마다 코드를 고쳐야 하므로
    .env 로 빼둡니다. Ollama 로 갈아끼울 때는 이 줄만 지우면 됩니다.
    """
    raw = os.environ.get("EXTRA_BODY", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        print("[config] EXTRA_BODY 가 올바른 JSON 이 아닙니다. 무시합니다.")
        return {}


EXTRA_BODY = _load_extra_body()


def _load_vlm_extra_body() -> dict:
    """사진 분석(SHOPPING_VLM_MODEL) 호출에만 쓰는 추가 파라미터. 비어 있으면 EXTRA_BODY 를 쓴다.

    에이전트 모델을 바꿔 가며 비교할 때 사진 분석 모델은 고정한다. 그런데 thinking 을 끄는
    방법은 모델마다 달라서(GLM 은 reasoning_effort=low, Gemma 는 enable_thinking=false)
    에이전트용 EXTRA_BODY 를 VLM 에 그대로 보내면 사진 분석 조건이 모델마다 달라진다.
    """
    raw = os.environ.get("SHOPPING_VLM_EXTRA_BODY", "").strip()
    if not raw:
        return EXTRA_BODY
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else EXTRA_BODY
    except json.JSONDecodeError:
        print("[config] SHOPPING_VLM_EXTRA_BODY 가 올바른 JSON 이 아닙니다. EXTRA_BODY 를 씁니다.")
        return EXTRA_BODY


SHOPPING_VLM_EXTRA_BODY = _load_vlm_extra_body()
