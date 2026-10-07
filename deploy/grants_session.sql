-- app_session 권한. session DB 에 적용한다. 다시 실행해도 된다.
GRANT USAGE ON SCHEMA public TO app_session;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON agent_state, image_search_queries, image_analyses TO app_session;
