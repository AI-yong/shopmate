-- 세션 DB(session) 스키마. 대화 상태·승인 대기·질의 이미지·사진 분석 결과.
--
--   psql "$SESSION_DSN" -v ON_ERROR_STOP=1 -f deploy/schema_session.sql
--
-- 매 요청 쓰이고 분~시간 뒤 버려지는 데이터라 테이블은 UNLOGGED 이고 expires_at 으로 만료한다.

CREATE UNLOGGED TABLE agent_state (
    user_id text NOT NULL,
    pending jsonb,
    rest jsonb DEFAULT '{}'::jsonb NOT NULL,
    turn integer DEFAULT 0 NOT NULL,
    expires_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE UNLOGGED TABLE image_analyses (
    analysis_id uuid NOT NULL,
    query_id uuid NOT NULL,
    user_id text NOT NULL,
    items jsonb NOT NULL,
    model_name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE UNLOGGED TABLE image_search_queries (
    query_id uuid NOT NULL,
    user_id text NOT NULL,
    object_bucket text NOT NULL,
    object_key text NOT NULL,
    content_sha256 text NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT image_search_queries_height_check CHECK ((height > 0)),
    CONSTRAINT image_search_queries_width_check CHECK ((width > 0))
);

ALTER TABLE ONLY agent_state
    ADD CONSTRAINT agent_state_pkey PRIMARY KEY (user_id);

ALTER TABLE ONLY image_analyses
    ADD CONSTRAINT image_analyses_pkey PRIMARY KEY (analysis_id);

ALTER TABLE ONLY image_search_queries
    ADD CONSTRAINT image_search_queries_object_key_key UNIQUE (object_key);

ALTER TABLE ONLY image_search_queries
    ADD CONSTRAINT image_search_queries_pkey PRIMARY KEY (query_id);

CREATE INDEX idx_agent_state_expires ON agent_state USING btree (expires_at) WHERE (expires_at IS NOT NULL);

CREATE INDEX idx_image_analyses_expires ON image_analyses USING btree (expires_at);

CREATE INDEX idx_image_analyses_owner ON image_analyses USING btree (user_id, created_at DESC);

CREATE INDEX idx_image_search_queries_expires ON image_search_queries USING btree (expires_at);

CREATE INDEX idx_image_search_queries_owner ON image_search_queries USING btree (user_id, created_at DESC);

ALTER TABLE ONLY image_analyses
    ADD CONSTRAINT image_analyses_query_id_fkey FOREIGN KEY (query_id) REFERENCES image_search_queries(query_id) ON DELETE CASCADE;
