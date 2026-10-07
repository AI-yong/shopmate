-- 웹 서버 전용 최소 권한 계정. 클러스터에 한 번 (postgres DB 에 붙어서).
--
--   psql "$MAINT_DSN" -v shop_pw="..." -v session_pw="..." -f deploy/roles.sql
--
-- 왜: 서버가 슈퍼유저(postgres)로 붙어 있어, 코드 한 줄의 실수(또는 SQL 주입)가 DB 전체
-- 권한이 됐다. 서버는 app_shop · app_session 으로만 붙고, 스키마를 만지는 배치
-- (임베딩·승격·마이그레이션)는 소유자 계정을 그대로 쓴다 (config.APP_SHOP_DSN 주석).
--
-- 권한은 DB 마다 따로다 → grants_shop.sql(shop) · grants_session.sql(session).
-- pg_hba.conf 가 trust 면 비밀번호는 검사되지 않는다. 권한 범위는 그래도 그대로 적용된다.

SELECT format('CREATE ROLE app_shop LOGIN PASSWORD %L', :'shop_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_shop') \gexec
SELECT format('CREATE ROLE app_session LOGIN PASSWORD %L', :'session_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_session') \gexec
