# 데이터베이스 준비

업무 DB(`shop`)와 세션 DB(`session`)를 따로 둡니다. 주문·재고는 영구·백업 대상이고,
대화 상태는 분~시간 뒤 버려지는 데이터라 수명주기가 다르기 때문입니다.
세션 테이블은 UNLOGGED라 한 PostgreSQL 인스턴스에 두 DB를 함께 둬도 됩니다.

| 파일 | 내용 |
|---|---|
| `schema_shop.sql` | 업무 DB 전체 스키마 — 상품·variant·재고·장바구니·주문·찜, 벡터, 선호, 이벤트 outbox, 카탈로그 스테이징 |
| `schema_session.sql` | 세션 DB 전체 스키마 — 대화 상태·질의 이미지·사진 분석 결과 |
| `roles.sql` | 웹 서버 전용 최소 권한 계정 `app_shop` · `app_session` |
| `grants_shop.sql` · `grants_session.sql` | 그 계정의 테이블 권한 |
| `docker-compose.yml` | 업무 DB(PostgreSQL + pgvector) · 세션 DB를 띄우는 예시 |

## 1. PostgreSQL 띄우기

이미 PostgreSQL(16 이상) + pgvector가 있다면 `shop`·`session` 두 DB만 만들면 됩니다.
Docker 예시는 업무 DB를 5432, 세션 DB를 5433에 띄우고, 첫 기동 때 두 스키마를 자동으로 적용합니다.

```bash
export SHOPDB_PASSWORD=... SESSIONDB_PASSWORD=...
docker compose -f deploy/docker-compose.yml up -d
```

이 compose를 쓰면 `.env`의 `SESSION_DSN`·`APP_SESSION_DSN` 포트를 5433으로 바꿉니다.

## 2. 스키마 적용

compose를 쓰지 않았다면 직접 적용합니다.

```bash
psql "$SHOP_DSN"    -v ON_ERROR_STOP=1 -f deploy/schema_shop.sql
psql "$SESSION_DSN" -v ON_ERROR_STOP=1 -f deploy/schema_session.sql
```

벡터 인덱스는 일부러 만들지 않습니다. 이 서비스의 질의는 거의 항상 카테고리·가격 필터가 붙는데,
필터 뒤 정확 검색이 HNSW + 필터보다 빨랐습니다. 이벤트 테이블의 월별 파티션은 `event_pipeline.py`가 필요할 때 만듭니다.

## 3. 웹 서버 전용 계정

웹 서버는 최소 권한 계정으로만 붙습니다. 상품 데이터를 넣는 작업은 소유자 계정(`SHOP_DSN`)으로 합니다.

```bash
psql "postgresql://postgres@127.0.0.1:5432/postgres" -v shop_pw="..." -v session_pw="..." -f deploy/roles.sql
psql "$SHOP_DSN"    -f deploy/grants_shop.sql
psql "$SESSION_DSN" -f deploy/grants_session.sql
```

`.env`의 `APP_SHOP_DSN`·`APP_SESSION_DSN`에 이 계정을 넣습니다.

## 4. 상품 데이터와 벡터

채워야 할 테이블은 [루트 README](../README.md#상품-데이터)에 정리되어 있습니다.
Qwen3-VL 상품 벡터가 상품의 95% 미만이면 Qwen 사진 검색은 오류를 내고 SigLIP + KURE로 폴백합니다
(`search_qwen3_vl.MIN_COVERAGE`).

## 5. 이벤트 워커

서버와 별도 프로세스로 실행합니다. 여러 워커가 떠도 `FOR UPDATE SKIP LOCKED`로 같은 행을 잡지 않습니다.

```bash
python event_pipeline.py          # 계속 소비
python event_pipeline.py --once   # 한 번만 비우기
```

실패 이벤트는 지수 백오프로 최대 8회 재시도한 뒤 `failed_at`에 격리합니다.
