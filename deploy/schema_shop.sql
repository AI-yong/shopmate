-- 업무 DB(shop) 스키마. PostgreSQL 16+ · pgvector.
--
--   psql "$SHOP_DSN" -v ON_ERROR_STOP=1 -f deploy/schema_shop.sql
--
-- 상품·색상 variant·재고·장바구니·주문·찜, 검색 벡터, 명시적 선호, 이벤트 outbox,
-- 카탈로그 스테이징을 담는다. 벡터 인덱스는 일부러 만들지 않는다 — 이 서비스의 질의는
-- 거의 항상 카테고리·가격 필터가 붙어, 필터 뒤 정확 검색이 HNSW + 필터보다 빨랐다.
-- 이벤트의 월별 파티션은 event_pipeline.py 가 필요할 때 만든다.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE cart_items (
    user_id text NOT NULL,
    variant_id text NOT NULL,
    size text NOT NULL,
    quantity integer NOT NULL,
    seq bigserial NOT NULL,
    CONSTRAINT cart_items_quantity_check CHECK ((quantity > 0))
);

CREATE TABLE category_groups (
    group_name text NOT NULL,
    category text NOT NULL,
    sort_order integer NOT NULL
);

CREATE TABLE demo_order_templates (
    order_id text NOT NULL,
    seq bigserial NOT NULL,
    variant_id text NOT NULL,
    size text NOT NULL,
    quantity integer NOT NULL,
    ordered_days_ago integer NOT NULL,
    shipped_days_ago integer,
    delivered_days_ago integer,
    cancelled_days_ago integer,
    returned_days_ago integer,
    status text NOT NULL,
    return_reason text,
    CONSTRAINT demo_order_templates_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT demo_order_templates_status_check CHECK ((status = ANY (ARRAY['배송 준비 중'::text, '배송 중'::text, '배송 완료'::text, '취소됨'::text, '반품 신청됨'::text])))
);

CREATE TABLE events (
    event_id uuid NOT NULL,
    occurred_at timestamp with time zone NOT NULL,
    event_type text NOT NULL,
    user_id text,
    session_id text,
    request_id uuid,
    recommendation_id uuid,
    experiment_id text,
    model_name text,
    model_version text,
    product_id text,
    rank integer,
    source text,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT events_rank_check CHECK (((rank IS NULL) OR (rank > 0)))
)
PARTITION BY RANGE (occurred_at);

CREATE TABLE events_default PARTITION OF events DEFAULT;

CREATE TABLE materials (
    material text NOT NULL,
    material_detail text NOT NULL,
    machine_washable boolean NOT NULL,
    care text NOT NULL
);

CREATE TABLE meta (
    key text NOT NULL,
    value text NOT NULL
);

CREATE TABLE orders (
    order_id text NOT NULL,
    user_id text NOT NULL,
    variant_id text NOT NULL,
    product_id text NOT NULL,
    product_name text NOT NULL,
    color text NOT NULL,
    size text NOT NULL,
    quantity integer NOT NULL,
    price integer NOT NULL,
    ordered_at date NOT NULL,
    shipped_at date,
    delivered_at date,
    cancelled_at date,
    returned_at date,
    status text NOT NULL,
    return_reason text,
    CONSTRAINT orders_quantity_check CHECK ((quantity > 0)),
    CONSTRAINT orders_status_check CHECK ((status = ANY (ARRAY['배송 준비 중'::text, '배송 중'::text, '배송 완료'::text, '취소됨'::text, '반품 신청됨'::text])))
);

CREATE TABLE outbox_events (
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    failed_at timestamp with time zone,
    CONSTRAINT outbox_events_attempts_check CHECK ((attempts >= 0))
);

CREATE TABLE product_media_embeddings (
    media_id text NOT NULL,
    model_name text NOT NULL,
    model_revision text NOT NULL,
    embedding_dim integer NOT NULL,
    embedding vector(768) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT product_media_embeddings_embedding_dim_check CHECK ((embedding_dim = 768))
);

CREATE TABLE product_media_staging (
    media_id text NOT NULL,
    source_item_id text NOT NULL,
    view_name text DEFAULT 'main'::text NOT NULL,
    object_bucket text NOT NULL,
    object_key text NOT NULL,
    thumbnail_key text NOT NULL
);

CREATE TABLE product_multimodal_embeddings (
    media_id text NOT NULL,
    model_name text NOT NULL,
    model_revision text NOT NULL,
    recipe text NOT NULL,
    embedding_dim integer NOT NULL,
    embedding vector(1536) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT product_multimodal_embeddings_embedding_dim_check CHECK ((embedding_dim = 1536)),
    CONSTRAINT product_multimodal_embeddings_recipe_check CHECK ((recipe = ANY (ARRAY['image_text'::text, 'image_search_text'::text])))
);

CREATE TABLE product_size_options (
    product_id text NOT NULL,
    size_code text NOT NULL,
    size_system text NOT NULL,
    display_label text NOT NULL,
    sort_order integer NOT NULL,
    source text NOT NULL,
    is_synthetic boolean DEFAULT false NOT NULL
);

CREATE TABLE product_stock (
    variant_id text NOT NULL,
    size text NOT NULL,
    stock integer NOT NULL,
    CONSTRAINT product_stock_stock_check CHECK ((stock >= 0))
);

CREATE TABLE product_variants (
    variant_id text NOT NULL,
    product_id text NOT NULL,
    color text NOT NULL,
    sort_order integer NOT NULL,
    is_synthetic boolean DEFAULT false NOT NULL
);

CREATE TABLE products (
    product_id text NOT NULL,
    name text NOT NULL,
    category text NOT NULL,
    gender text NOT NULL,
    brand text NOT NULL,
    price integer NOT NULL,
    rating real NOT NULL,
    review_count integer NOT NULL,
    description text NOT NULL,
    material text NOT NULL,
    material_detail text NOT NULL,
    care text NOT NULL,
    machine_washable boolean NOT NULL,
    delivery_days integer NOT NULL,
    source_name text DEFAULT 'seed'::text NOT NULL,
    source_item_id text,
    size_system text DEFAULT 'legacy_numeric'::text NOT NULL,
    embedding vector(1024),
    wish_base integer DEFAULT 0 NOT NULL,
    CONSTRAINT products_gender_check CHECK ((gender = ANY (ARRAY['남성'::text, '여성'::text, '공용'::text]))),
    CONSTRAINT products_price_check CHECK ((price >= 0)),
    CONSTRAINT products_wish_base_check CHECK ((wish_base >= 0))
);

CREATE TABLE user_preferences (
    user_id text NOT NULL,
    preference_key text NOT NULL,
    preference_values text[] NOT NULL,
    source text DEFAULT 'explicit'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT user_preferences_preference_key_check CHECK ((preference_key = ANY (ARRAY['gender'::text, 'shoe_size'::text, 'top_size'::text, 'bottom_size'::text, 'preferred_colors'::text, 'preferred_materials'::text, 'avoided_materials'::text, 'preferred_styles'::text, 'usual_budget'::text]))),
    CONSTRAINT user_preferences_preference_values_check CHECK (((cardinality(preference_values) >= 1) AND (cardinality(preference_values) <= 5))),
    CONSTRAINT user_preferences_source_check CHECK ((source = 'explicit'::text))
);

CREATE TABLE users (
    user_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE web_sessions (
    session_id uuid NOT NULL,
    token_sha256 bytea NOT NULL,
    user_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT web_sessions_check CHECK ((expires_at > created_at)),
    CONSTRAINT web_sessions_token_sha256_check CHECK ((octet_length(token_sha256) = 32))
);

CREATE TABLE wishlists (
    user_id text NOT NULL,
    product_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY cart_items
    ADD CONSTRAINT cart_items_pkey PRIMARY KEY (user_id, variant_id, size);

ALTER TABLE ONLY category_groups
    ADD CONSTRAINT category_groups_pkey PRIMARY KEY (group_name, category);

ALTER TABLE ONLY demo_order_templates
    ADD CONSTRAINT demo_order_templates_pkey PRIMARY KEY (order_id);

ALTER TABLE ONLY events
    ADD CONSTRAINT events_pkey PRIMARY KEY (event_id, occurred_at);

ALTER TABLE ONLY materials
    ADD CONSTRAINT materials_pkey PRIMARY KEY (material);

ALTER TABLE ONLY meta
    ADD CONSTRAINT meta_pkey PRIMARY KEY (key);

ALTER TABLE ONLY orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (order_id);

ALTER TABLE ONLY outbox_events
    ADD CONSTRAINT outbox_events_idempotency_key_key UNIQUE (idempotency_key);

ALTER TABLE ONLY outbox_events
    ADD CONSTRAINT outbox_events_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY product_media_embeddings
    ADD CONSTRAINT product_media_embeddings_pkey PRIMARY KEY (media_id, model_name, model_revision);

ALTER TABLE ONLY product_media_staging
    ADD CONSTRAINT product_media_staging_pkey PRIMARY KEY (media_id);

ALTER TABLE ONLY product_media_staging
    ADD CONSTRAINT product_media_staging_source_item_id_view_name_key UNIQUE (source_item_id, view_name);

ALTER TABLE ONLY product_multimodal_embeddings
    ADD CONSTRAINT product_multimodal_embeddings_pkey PRIMARY KEY (media_id, model_name, model_revision, recipe);

ALTER TABLE ONLY product_size_options
    ADD CONSTRAINT product_size_options_pkey PRIMARY KEY (product_id, size_code);

ALTER TABLE ONLY product_stock
    ADD CONSTRAINT product_stock_pkey PRIMARY KEY (variant_id, size);

ALTER TABLE ONLY product_variants
    ADD CONSTRAINT product_variants_pkey PRIMARY KEY (variant_id);

ALTER TABLE ONLY product_variants
    ADD CONSTRAINT product_variants_product_id_color_key UNIQUE (product_id, color);

ALTER TABLE ONLY products
    ADD CONSTRAINT products_pkey PRIMARY KEY (product_id);

ALTER TABLE ONLY products
    ADD CONSTRAINT products_source_name_source_item_id_key UNIQUE (source_name, source_item_id);

ALTER TABLE ONLY user_preferences
    ADD CONSTRAINT user_preferences_pkey PRIMARY KEY (user_id, preference_key);

ALTER TABLE ONLY users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);

ALTER TABLE ONLY web_sessions
    ADD CONSTRAINT web_sessions_pkey PRIMARY KEY (session_id);

ALTER TABLE ONLY web_sessions
    ADD CONSTRAINT web_sessions_token_sha256_key UNIQUE (token_sha256);

ALTER TABLE ONLY wishlists
    ADD CONSTRAINT wishlists_pkey PRIMARY KEY (user_id, product_id);

CREATE INDEX idx_events_product_time ON ONLY events USING btree (product_id, occurred_at DESC);

CREATE INDEX idx_events_recommendation ON ONLY events USING btree (recommendation_id, rank);

CREATE INDEX idx_events_user_time ON ONLY events USING btree (user_id, occurred_at DESC);

CREATE INDEX idx_cart_items_seq ON cart_items USING btree (user_id, seq);

CREATE INDEX idx_media_embeddings_model ON product_media_embeddings USING btree (model_name, model_revision);

CREATE INDEX idx_orders_ordered_at ON orders USING btree (ordered_at);

CREATE INDEX idx_orders_status ON orders USING btree (status);

CREATE INDEX idx_orders_user_time ON orders USING btree (user_id, ordered_at DESC);

CREATE INDEX idx_outbox_events_pending ON outbox_events USING btree (available_at, occurred_at) WHERE ((processed_at IS NULL) AND (failed_at IS NULL));

CREATE INDEX idx_product_mm_embeddings_model ON product_multimodal_embeddings USING btree (model_name, model_revision, recipe);

CREATE INDEX idx_products_brand ON products USING btree (brand);

CREATE INDEX idx_products_category ON products USING btree (category);

CREATE INDEX idx_size_options_system ON product_size_options USING btree (size_system, size_code);

CREATE INDEX idx_variants_color ON product_variants USING btree (color);

CREATE INDEX idx_variants_product ON product_variants USING btree (product_id);

CREATE INDEX idx_web_sessions_expires ON web_sessions USING btree (expires_at);

CREATE INDEX idx_web_sessions_user ON web_sessions USING btree (user_id) WHERE (revoked_at IS NULL);

CREATE INDEX idx_wishlists_product ON wishlists USING btree (product_id);

ALTER TABLE ONLY cart_items
    ADD CONSTRAINT cart_items_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id);

ALTER TABLE ONLY cart_items
    ADD CONSTRAINT cart_items_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id);

ALTER TABLE ONLY demo_order_templates
    ADD CONSTRAINT demo_order_templates_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id);

ALTER TABLE ONLY orders
    ADD CONSTRAINT orders_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(product_id);

ALTER TABLE ONLY orders
    ADD CONSTRAINT orders_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id);

ALTER TABLE ONLY orders
    ADD CONSTRAINT orders_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id);

ALTER TABLE ONLY product_media_embeddings
    ADD CONSTRAINT product_media_embeddings_media_id_fkey FOREIGN KEY (media_id) REFERENCES product_media_staging(media_id) ON DELETE CASCADE;

ALTER TABLE ONLY product_multimodal_embeddings
    ADD CONSTRAINT product_multimodal_embeddings_media_id_fkey FOREIGN KEY (media_id) REFERENCES product_media_staging(media_id) ON DELETE CASCADE;

ALTER TABLE ONLY product_size_options
    ADD CONSTRAINT product_size_options_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE;

ALTER TABLE ONLY product_stock
    ADD CONSTRAINT product_stock_variant_id_fkey FOREIGN KEY (variant_id) REFERENCES product_variants(variant_id);

ALTER TABLE ONLY product_variants
    ADD CONSTRAINT product_variants_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(product_id);

ALTER TABLE ONLY user_preferences
    ADD CONSTRAINT user_preferences_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY web_sessions
    ADD CONSTRAINT web_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;

ALTER TABLE ONLY wishlists
    ADD CONSTRAINT wishlists_product_id_fkey FOREIGN KEY (product_id) REFERENCES products(product_id) ON DELETE CASCADE;

ALTER TABLE ONLY wishlists
    ADD CONSTRAINT wishlists_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
