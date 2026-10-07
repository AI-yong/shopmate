-- app_shop 권한. 업무 DB(shop)에 적용한다.
-- 다시 실행해도 된다.
--
-- 읽기는 전부, 쓰기는 웹 서버가 실제로 쓰는 테이블만. 테이블을 만들거나 바꾸는 권한은 없다.
-- events 는 이벤트 워커(event_pipeline.py, 소유자 계정)만 쓴다.

GRANT USAGE ON SCHEMA public TO app_shop;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_shop;

GRANT INSERT, UPDATE, DELETE ON cart_items, orders, web_sessions, user_preferences TO app_shop;
GRANT INSERT, DELETE ON wishlists TO app_shop;                   -- v13 찜
GRANT INSERT ON users, outbox_events TO app_shop;              -- ON CONFLICT DO NOTHING 은 INSERT 만
GRANT UPDATE ON product_stock TO app_shop;                     -- 결제 차감 · 취소 원복
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_shop;

-- 실험·배치가 나중에 만드는 테이블(새 벡터 테이블 등)도 읽을 수 있게. 쓰기는 주지 않는다.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO app_shop;
