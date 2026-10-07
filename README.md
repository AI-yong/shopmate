# SHOPMATE — 멀티모달 쇼핑 에이전트

한국어 대화와 사진으로 상품을 찾고, 장바구니·주문·취소·반품까지 이어 주는 쇼핑 에이전트입니다.
LLM은 정해진 Tool만 호출하고, 결제처럼 되돌릴 수 없는 작업은 화면의 승인 버튼을 눌러야 실행됩니다.

- **대화형 검색**: "5만원 이하 검은 니트", "이거랑 비슷한데 더 밝은 색" 같은 요청을 SQL 조건과 의미 검색으로 나눠 처리합니다.
- **사진 검색**: 업로드한 사진에서 VLM이 아이템과 위치를 찾고, 고른 아이템을 잘라 Qwen3-VL 멀티모달 임베딩으로 비슷한 상품을 찾습니다.
- **안전한 상태 변경**: 결제·취소·반품은 확인 대기 → 버튼 승인 → 재검증을 거쳐 실행합니다. 승인은 만료되고, 같은 요청을 두 번 실행하지 않습니다.
- **결과 검증**: Tool 결과를 원래 요청과 대조해 빠진 조건은 한 번만 자동으로 보완하고, 모델이 지어낸 상품 목록은 막습니다.

## 구조

```
src/shopmate/
├─ server.py            HTTP API·세션·사진 업로드·승인 (web/ 화면도 함께 낸다)
├─ config.py            .env 설정
├─ agent/
│  ├─ loop.py           모델 호출 반복, Tool 실행, 승인 대기·재개
│  ├─ tools.py          LLM 에 노출하는 Tool 스키마·구현·인자 검증
│  └─ verifier.py       결과를 요청과 대조해 안전한 누락만 보완
├─ store/
│  ├─ shop.py           쇼핑몰 규칙 — 재고·장바구니·결제·취소·반품
│  ├─ db.py             업무 DB (PostgreSQL + pgvector)
│  ├─ session_state.py  세션 DB (대화 상태·승인 대기)
│  └─ events.py         transactional outbox 이벤트 워커
└─ search/
   ├─ routing.py        사진 검색 경로 선택과 폴백
   ├─ filter_rules.py   사용자가 직접 말한 조건만 하드 필터로 남기는 판정
   ├─ text_embedding.py 글 검색 임베딩 (KURE-v1)
   ├─ photo_analysis.py 사진 분석(VLM)·아이템 선택·크롭
   ├─ photo_store.py    질의 사진 검사·저장 (MinIO)
   ├─ photo_query.py    사진 검색문 조립
   ├─ qwen.py           Qwen3-VL 사진 검색 (임베딩 서비스 호출 + 벡터 검색)
   ├─ siglip.py         SigLIP 2 사진 검색 (폴백)
   └─ fallback.py       SigLIP + KURE 순위 결합(RRF) 폴백
services/qwen3_vl/      Qwen3-VL 임베딩 서비스 (별도 가상환경, :8092)
web/                    화면 (빌드 없는 ES 모듈)
deploy/                 DB 스키마·권한·docker-compose
```

| 저장소 | 담는 것 |
|---|---|
| 업무 DB (`shop`) | 상품·색상 variant·재고·장바구니·주문·찜, 검색 벡터, 명시적 선호, 이벤트 outbox |
| 세션 DB (`session`) | 대화 상태·승인 대기·질의 이미지 메타데이터·사진 분석 결과 (TTL로 만료) |
| MinIO | 상품 이미지와 사용자가 올린 질의 이미지 파일 |

## 검색 경로

| 요청 | 경로 |
|---|---|
| 글만 | 사용자가 말한 조건 → SQL 하드 필터, 나머지 의미 → KURE-v1 텍스트 임베딩 |
| 사진만 | 아이템 선택·크롭 → Qwen3-VL 이미지 벡터 → 상품 문서 벡터 |
| 사진 + 글 | 크롭한 사진 + 영어 검색문 → Qwen3-VL fused 벡터 → 같은 상품 벡터 |
| Qwen 서비스 장애 | SigLIP 2 이미지 검색 + KURE 텍스트 검색을 RRF로 결합 |

- 경로 선택과 폴백은 `search/routing.py` 한 곳에서 정합니다.
- **사용자가 직접 말한 값만 하드 필터**가 됩니다(`search/filter_rules.py`). 사진에서 VLM이 추정한 색·소재는 필터로 쓰지 않고 사진 벡터로 반영합니다. "검은색 말고"처럼 부정한 값은 제외 필터가 됩니다.
- 사진에 아이템이 여러 개면 면적이 압도적인 아이템으로 진행하고, 비슷하면 사용자에게 고르게 합니다(`search/photo_analysis.py`). 사용자가 품목을 말하지 않았으면 고른 아이템의 대분류(상의·하의…)로 후보를 제한하고, 0건이면 그 제한만 풉니다.
- 상품 쪽 벡터는 Qwen3-VL 문서 벡터 한 벌(사진 + 검색용 텍스트)을 세 경로가 공유합니다. 벡터가 상품의 95% 미만이면 Qwen 검색은 오류를 내고 폴백합니다.

## 요구 사항

- Python 3.12
- PostgreSQL 16 이상 + [pgvector](https://github.com/pgvector/pgvector)
- MinIO (또는 S3 호환 저장소)
- OpenAI 호환 `/v1/chat/completions` 서버 — Tool 호출을 지원하는 LLM과 이미지 입력을 받는 VLM
- Qwen3-VL 임베딩 서비스용 별도 가상환경 (Apple Silicon `mps` 또는 CUDA 권장)

## 실행

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # DB·MinIO·모델 서버 주소를 채운다
```

DB 스키마(업무·세션 DB 각 파일 하나)와 권한은 [deploy/README.md](deploy/README.md) 순서대로 적용합니다.
상품 데이터는 아래 "상품 데이터" 절을 참고해 직접 넣어야 합니다.

Qwen3-VL 임베딩 서비스는 별도 환경에서 띄웁니다. 서비스가 없으면 사진 검색은 SigLIP + KURE로 폴백합니다.

```bash
python3 -m venv .venv-qwen
.venv-qwen/bin/pip install -r services/qwen3_vl/requirements.txt
.venv-qwen/bin/python services/qwen3_vl/server.py --device auto
```

웹 서버:

```bash
.venv/bin/python -m shopmate.server
# http://127.0.0.1:8000
```

검색 노출·주문 이벤트를 적재하는 워커는 별도 프로세스입니다.

```bash
.venv/bin/python -m shopmate.store.events
```

## 상품 데이터

상품 데이터와 그것을 만든 구축 스크립트는 저장소에 포함되어 있지 않습니다([DATA_USAGE.md](DATA_USAGE.md)).
개발 중에는 연구용 Amazon Reviews 2023 Fashion 부분집합으로 상품 약 1만 5천 개를 만들어 썼습니다.
서버를 실행하려면 `deploy/schema_shop.sql` 구조에 맞춰 다음을 채워야 합니다.

| 무엇 | 어디 |
|---|---|
| 상품·분류·소재·색상 variant·사이즈·재고 | `products`, `category_groups`, `materials`, `product_variants`, `product_size_options`, `product_stock` |
| 상품 사진 (MinIO 객체 위치) | `product_media_staging` |
| 글 검색 벡터 (KURE-v1, 1024차원) | `products.embedding`, `meta` 테이블의 `embed_model` 값 |
| 사진 검색 벡터 (Qwen3-VL, 1536차원) | `product_multimodal_embeddings` (`QWEN3_VL_RECIPE` 조리법) |
| 폴백 사진 벡터 (SigLIP 2, 768차원) | `product_media_embeddings` |
| 데모 주문 템플릿 (선택) | `demo_order_templates` |

## 데이터와 라이선스

이 저장소에는 코드와 스키마만 있습니다. 상품 메타데이터와 이미지는 포함하지 않으며,
외부 데이터 사용 조건은 [DATA_USAGE.md](DATA_USAGE.md)를 따릅니다.
