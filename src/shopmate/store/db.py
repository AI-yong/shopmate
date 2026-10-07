"""PostgreSQL 저장 계층.

이 모듈이 아는 것은 "어떻게 저장하는가" 뿐입니다.
"이 주문을 취소할 수 있는가" 같은 정책 판단은 store/shop.py 에 둡니다.
판단하는 곳이 둘이 되면 둘은 반드시 어긋나기 때문입니다.
그래서 여기에는 트리거도 뷰도 없습니다.

데이터를 두 종류로 나눕니다.

  카탈로그   products · product_variants · materials · category_groups
  상태       product_stock · cart_items · orders · wishlists

저장 방식에서 정해 둔 것

  embedding        vector(1024). HNSW 인덱스는 일부러 안 만든다
                   (측정: 필터 붙은 질의에서 10만 개 기준 64배 느림).
  날짜             date. 이 도메인은 시각이 아니라 날짜다. 읽을 때는 isoformat()
                   문자열로 돌려준다 — store/shop.py 가 문자열로 비교한다.
  줄 순서          명시적 seq(bigserial). "두 번째 거 담아줘" 가 화면 순서와 같아야 한다.
  결제 잠금        권고 잠금(pg_advisory_xact_lock). 주문번호 발급을 한 줄로 세운다.
  에이전트 상태     여기 없다. 수명주기가 달라 세션 DB 로 분리했다 (store/session_state.py).
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from datetime import date, timedelta

import psycopg

from shopmate import config

# 기본 사용자. 세션이 없는 곳(터미널 실행 등)은 이 사용자로 동작합니다.
DEMO_USER_ID = "demo"

# 접속 정보는 config.py 가 .env 에서 읽습니다 (설정은 한 곳에).
SHOP_DSN = config.SHOP_DSN          # 업무 DB


# --------------------------------------------------------------------------
# 행 — 이름으로도 순번으로도 읽는다
# --------------------------------------------------------------------------

class Row:
    """row["name"] 과 row[0] 을 둘 다 받는다. 호출부가 두 방식을 섞어 씁니다."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        try:
            return self._vals[self._cols.index(key)]
        except ValueError:
            raise KeyError(key) from None

    def __iter__(self):
        return iter(self._vals)          # 튜플처럼 값을 돈다

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self):
        return f"Row({dict(zip(self._cols, self._vals))})"


def _row_factory(cursor):
    cols = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(cols, values)


def _size_sort_key(value):
    """FREE·문자·숫자 사이즈를 사람이 기대하는 순서로 정렬한다."""
    text = str(value)
    alpha = {"XXS": 0, "XS": 1, "S": 2, "M": 3, "L": 4,
             "XL": 5, "2XL": 6, "XXL": 6, "3XL": 7}
    if text == "FREE":
        return (0, 0)
    if text == "ONE_SIZE":
        return (0, 1)
    if text in alpha:
        return (1, alpha[text])
    if text.isdigit():
        return (2, int(text))
    return (3, text)


# --------------------------------------------------------------------------
# 커넥션
# --------------------------------------------------------------------------

def connect(dsn=None):
    """커넥션을 연다.

    lock_timeout 을 건다. 무한정 기다리다 요청이 통째로 멎는 것보다,
    5초에 실패하고 사용자에게 말하는 편이 낫다.
    """
    conn = psycopg.connect(dsn or SHOP_DSN, row_factory=_row_factory)
    # pgvector 열을 numpy 배열로 받게 한다. 등록하지 않으면 문자열로 온다.
    try:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    except Exception:
        pass        # 벡터를 안 쓰는 배치에서는 없어도 된다
    conn.execute("SET lock_timeout = '5s'")
    conn.execute("SET statement_timeout = '30s'")
    conn.commit()
    return conn


def end_read_transaction(conn):
    """연결에 열린 트랜잭션을 닫는다. 쓰기는 모두 제자리에서 commit 하므로 남아 있는 것은
    읽기뿐이라 commit 해도 새로 확정되는 쓰기는 없다. 실패한 문장 뒤(INERROR)라면 rollback 해야
    연결이 다시 쓰인다. 안 닫으면 "idle in transaction" 으로 잠금을 쥐어 ALTER TABLE·VACUUM 을
    막는다(2026-09-29). 반드시 그 연결의 db_lock 을 쥔 쪽에서 부른다 — 다른 스레드가 여러 문장을
    한 트랜잭션으로 묶는 중에 부르면 그 트랜잭션을 중간에 확정해 버린다."""
    if conn is None or conn.closed:
        return
    try:
        status = conn.info.transaction_status
        if status == psycopg.pq.TransactionStatus.INTRANS:
            conn.commit()
        elif status == psycopg.pq.TransactionStatus.INERROR:
            conn.rollback()
    except Exception as problem:
        print(f"[DB] 트랜잭션 정리 실패: {problem}")


def lock_checkout(conn):
    """결제·취소 트랜잭션을 한 줄로 세운다.

    결제는 "주문번호를 뽑고 → 재고를 차감하고 → 주문을 넣는" 세 걸음입니다.
    PostgreSQL 은 쓰기가 서로를 막지 않으므로, 이 세 걸음을 권고 잠금으로
    한 줄로 세웁니다. 트랜잭션이 끝나면 자동으로 풀립니다(xact).
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext('checkout'))")


# --------------------------------------------------------------------------
# 사용자
# --------------------------------------------------------------------------

def new_user_id():
    """새 사용자 ID. 쿠키에는 담지 않는다 — 쿠키에는 create_web_session() 의 토큰이 간다."""
    return "u_" + secrets.token_urlsafe(12)


def ensure_user(conn, user_id):
    """사용자 행을 만든다. 이미 있으면 그대로. 새로 만들었으면 True."""
    cur = conn.execute(
        "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
        (user_id,),
    )
    conn.commit()
    return cur.rowcount == 1


# --------------------------------------------------------------------------
# 세션 토큰 — 쿠키에 담는 값
# --------------------------------------------------------------------------
#
# 예전에는 쿠키 sid 가 곧 user_id 였다. user_id 는 이벤트·로그에 평문으로 남으므로
# 그걸 본 사람은 누구든 그 사용자가 될 수 있었다. 이제 쿠키에는 랜덤 토큰을 주고
# DB 에는 sha256 만 둔다 — DB 를 읽어도 쿠키를 만들 수 없다.

def _token_hash(token):
    return hashlib.sha256(token.encode()).digest()


def create_web_session(conn, user_id, max_age_seconds):
    """user_id 의 새 세션을 만들고 쿠키에 담을 토큰을 돌려준다. 사용자 행이 있어야 한다."""
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO web_sessions (session_id, token_sha256, user_id, expires_at)"
        " VALUES (%s, %s, %s, now() + %s * interval '1 second')",
        (uuid.uuid4(), _token_hash(token), user_id, max_age_seconds),
    )
    conn.commit()
    return token


def resolve_web_session(conn, token):
    """쿠키 토큰의 user_id. 없거나 만료·폐기됐으면 None."""
    if not token or len(token) > 128:
        return None
    row = conn.execute(
        "SELECT user_id FROM web_sessions WHERE token_sha256 = %s"
        " AND revoked_at IS NULL AND expires_at > now()",
        (_token_hash(token),),
    ).fetchone()
    return row["user_id"] if row else None


# --------------------------------------------------------------------------
# 찜 (v13) — 사용자·상품 한 쌍이 한 행. 상품의 찜 수 = products.wish_base + 행 수
# --------------------------------------------------------------------------

def wish_counts(conn, product_ids=None):
    """상품별 실제 찜 행 수. product_ids 를 주면 그 상품만. 찜이 없는 상품은 빠진다."""
    if product_ids is None:
        rows = conn.execute("SELECT product_id, count(*) FROM wishlists GROUP BY product_id")
    else:
        rows = conn.execute(
            "SELECT product_id, count(*) FROM wishlists WHERE product_id = ANY(%s)"
            " GROUP BY product_id", (list(product_ids),))
    counts = {row[0]: row[1] for row in rows}
    conn.commit()        # 읽기만 해도 트랜잭션이 열린 채 남지 않게 (idle in transaction 은 DDL 을 막는다)
    return counts


def user_wishlist(conn, user_id):
    """사용자가 찜한 상품 id, 최근 것부터."""
    ids = [row[0] for row in conn.execute(
        "SELECT product_id FROM wishlists WHERE user_id = %s ORDER BY created_at DESC",
        (user_id,))]
    conn.commit()
    return ids


def set_wish(conn, user_id, product_id, on):
    """찜을 켜거나 끈다. 상태가 실제로 바뀌었으면 True (이미 그 상태면 False)."""
    if on:
        cur = conn.execute(
            "INSERT INTO wishlists (user_id, product_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (user_id, product_id))
    else:
        cur = conn.execute(
            "DELETE FROM wishlists WHERE user_id = %s AND product_id = %s", (user_id, product_id))
    conn.commit()
    return cur.rowcount == 1


def revoke_web_session(conn, token):
    """쿠키 토큰의 세션을 폐기한다(사용자 전환 때 이전 세션). 폐기했으면 True."""
    if not token or len(token) > 128:
        return False
    cur = conn.execute(
        "UPDATE web_sessions SET revoked_at = now()"
        " WHERE token_sha256 = %s AND revoked_at IS NULL",
        (_token_hash(token),),
    )
    conn.commit()
    return cur.rowcount == 1


def purge_expired_web_sessions(conn):
    cur = conn.execute("DELETE FROM web_sessions WHERE expires_at < now()")
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# 명시적 선호 — 사용자가 "기억해 줘" 라고 한 것만
# --------------------------------------------------------------------------
#
# verifier.PreferenceMemory.entries 와 같은 모양 {key: {"values": [...], "updated_at": epoch}}
# 으로 주고받는다. 무엇을 저장할 수 있는가는 PreferenceMemory 가 판단하고,
# 테이블의 CHECK 는 그 판단이 새어 나갔을 때의 마지막 방어선이다.

def fetch_preferences(conn, user_id):
    return {
        row["preference_key"]: {"values": list(row["preference_values"]),
                                "updated_at": row["updated_at"].timestamp()}
        for row in conn.execute(
            "SELECT preference_key, preference_values, updated_at"
            " FROM user_preferences WHERE user_id = %s", (user_id,))
    }


def save_preferences(conn, user_id, entries):
    """entries 를 그 사용자의 선호 전체로 삼는다 (없는 키는 지운다)."""
    keys = list(entries)
    conn.execute(
        "DELETE FROM user_preferences WHERE user_id = %s"
        " AND NOT (preference_key = ANY(%s))", (user_id, keys))
    for key, entry in entries.items():
        conn.execute(
            "INSERT INTO user_preferences (user_id, preference_key, preference_values,"
            " updated_at) VALUES (%s, %s, %s, to_timestamp(%s))"
            " ON CONFLICT (user_id, preference_key) DO UPDATE SET"
            " preference_values = EXCLUDED.preference_values,"
            " updated_at = EXCLUDED.updated_at",
            (user_id, key, list(entry["values"]),
             float(entry.get("updated_at") or time.time())),
        )
    conn.commit()


def next_order_number(conn):
    """다음 주문 번호(정수). 모든 사용자를 통틀어 유일해야 하므로 DB 전체에서 본다.

    checkout 은 lock_checkout() 안에서 이걸 부르므로, 두 사용자가 동시에
    결제해도 권고 잠금이 번호 발급을 한 줄로 세웁니다.
    """
    row = conn.execute(
        "SELECT MAX(CAST(substr(order_id, 5) AS integer)) FROM orders"
        " WHERE order_id LIKE 'ORD-%%'"
    ).fetchone()
    return (row[0] or 1000) + 1


# --------------------------------------------------------------------------
# 재고 — 확인과 차감을 한 문장으로
# --------------------------------------------------------------------------

def take_stock(conn, variant_id, size, quantity):
    """재고를 차감한다. 성공하면 True.

    조건을 UPDATE 안에 넣어 한 문장으로 만듭니다. 확인과 차감 사이가 열려 있지
    않으므로 TOCTOU 가 없습니다. rowcount 가 0이면 조건이 안 맞은 것입니다.
    """
    cur = conn.execute(
        "UPDATE product_stock SET stock = stock - %s"
        " WHERE variant_id = %s AND size = %s AND stock >= %s",
        (quantity, variant_id, str(size), quantity),
    )
    return cur.rowcount == 1


def give_back_stock(conn, variant_id, size, quantity):
    """취소로 재고를 되돌린다."""
    cur = conn.execute(
        "UPDATE product_stock SET stock = stock + %s"
        " WHERE variant_id = %s AND size = %s",
        (quantity, variant_id, str(size)),
    )
    return cur.rowcount == 1


# --------------------------------------------------------------------------
# 읽기 — store/shop.py 가 메모리 구조를 채울 때 쓴다
# --------------------------------------------------------------------------

def fetch_media(conn):
    """상품별 대표 사진 위치. {product_id: {"bucket", "key", "thumbnail"}}

    사진은 MinIO 에 있고 DB 에는 위치만 있다. 사진은 products.source_item_id 로
    이어지므로 그 값이 없는 상품은 여기 없고, 화면은 그때 그림(art.js)으로 대신 그린다."""
    media = {}
    for row in conn.execute(
        "SELECT p.product_id, m.object_bucket, m.object_key, m.thumbnail_key"
        " FROM products p JOIN product_media_staging m"
        "   ON m.source_item_id = p.source_item_id"
        " WHERE p.source_item_id IS NOT NULL AND m.view_name = 'main'"
    ):
        media[row["product_id"]] = {
            "bucket": row["object_bucket"],
            "key": row["object_key"],
            "thumbnail": row["thumbnail_key"],
        }
    return media


def fetch_products(conn):
    """상품을 store/shop.py 가 쓰는 모양으로 돌려준다.

    색상이 variant 로 내려갔으므로 모양이 하나 늘었다.

        "sizes"   {270: 12, ...}          모든 색을 합친 재고. 기존 코드가 쓰던 자리다.
                                          "이 상품 270 있나" 는 색을 안 따지므로 그대로 둔다.
        "colors"  {"검은색": {"variant_id": "P001-BLK", "sizes": {270: 3, ...}}, ...}
                                          색별 상세. 담을 때는 여기를 봐야 한다.

    옛 "color" 키는 없다 — 상품에 색이 하나라는 전제가 사라졌기 때문이다.
    """
    by_variant = {}
    for row in conn.execute(
        "SELECT ps.variant_id, ps.size, ps.stock FROM product_stock ps"
        " JOIN product_variants pv ON pv.variant_id = ps.variant_id"
        " LEFT JOIN product_size_options so"
        " ON so.product_id = pv.product_id AND so.size_code = ps.size"
        " ORDER BY ps.variant_id, COALESCE(so.sort_order, 9999), ps.size"
    ):
        by_variant.setdefault(row["variant_id"], {})[row["size"]] = row["stock"]

    colors_by_product = {}
    for row in conn.execute(
        "SELECT variant_id, product_id, color FROM product_variants"
        " ORDER BY product_id, sort_order"
    ):
        colors_by_product.setdefault(row["product_id"], {})[row["color"]] = {
            "variant_id": row["variant_id"],
            "sizes": by_variant.get(row["variant_id"], {}),
        }

    size_options_by_product = {}
    for row in conn.execute(
        "SELECT product_id,size_code,size_system,display_label,source,is_synthetic"
        " FROM product_size_options ORDER BY product_id,sort_order,size_code"
    ):
        size_options_by_product.setdefault(row["product_id"], []).append({
            "code": row["size_code"],
            "system": row["size_system"],
            "label": row["display_label"],
            "source": row["source"],
            "is_synthetic": bool(row["is_synthetic"]),
        })

    media_by_product = fetch_media(conn)

    products = []
    for row in conn.execute(
        "SELECT product_id, name, category, gender, brand, price, rating,"
        " review_count, description, material, material_detail,"
        " care, machine_washable, delivery_days, wish_base FROM products ORDER BY product_id"
    ):
        colors = colors_by_product.get(row["product_id"], {})
        totals = {}
        for info in colors.values():
            for size, qty in info["sizes"].items():
                totals[size] = totals.get(size, 0) + qty
        products.append({
            "id": row["product_id"],
            "name": row["name"],
            "category": row["category"],
            "gender": row["gender"],
            "brand": row["brand"],
            "price": row["price"],
            "colors": colors,
            "sizes": dict(sorted(totals.items(), key=lambda pair: _size_sort_key(pair[0]))),
            "size_options": size_options_by_product.get(row["product_id"], []),
            "rating": row["rating"],
            "review_count": row["review_count"],
            "description": row["description"],
            "material": row["material"],
            "material_detail": row["material_detail"],
            "care": row["care"],
            "machine_washable": bool(row["machine_washable"]),
            "delivery_days": row["delivery_days"],
            # 찜 수의 합성 시작값(v13). 보이는 찜 수 = wish_base + wishlists 행 수.
            "wish_base": row["wish_base"],
            # 없으면 None. 화면이 그림으로 대신 그린다.
            "media": media_by_product.get(row["product_id"]),
        })
    return products


def _product_filter_where(product_name=None, group=None, category=None, gender=None,
                          brand=None, max_price=None, min_price=None, color=None,
                          size=None, material=None, machine_washable=None,
                          in_stock=None, exclude_category=None,
                          exclude_color=None, exclude_material=None):
    """상품 필터의 WHERE와 바인딩 값을 한 곳에서 만든다."""
    clauses = []
    params = []
    if product_name is not None:
        # 부분 일치의 %/_는 검색 문법이 아니라 사용자 입력 자체로 취급한다.
        needle = product_name.lower().replace("\\", "\\\\")
        needle = needle.replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(p.name) LIKE %s ESCAPE '\\'")
        params.append(f"%{needle}%")
    if group is not None:
        clauses.append(
            "EXISTS (SELECT 1 FROM category_groups cg"
            " WHERE cg.category = p.category AND cg.group_name = %s)"
        )
        params.append(group)
    if category is not None:
        clauses.append("p.category = %s")
        params.append(category)
    if exclude_category is not None:
        clauses.append("p.category IS DISTINCT FROM %s")
        params.append(exclude_category)
    if gender is not None:
        clauses.append("p.gender IN (%s, '공용')")
        params.append(gender)
    if brand is not None:
        clauses.append("p.brand = %s")
        params.append(brand)
    if max_price is not None:
        clauses.append("p.price <= %s")
        params.append(max_price)
    if min_price is not None:
        clauses.append("p.price >= %s")
        params.append(min_price)
    if color is not None and size is not None:
        # 색과 사이즈를 따로 EXISTS로 검사하면 "검은색은 품절, 파란색 100은 재고"
        # 인 상품도 검은색 100 재고가 있는 것처럼 통과한다. 같은 SKU에서 확인한다.
        clauses.append(
            "EXISTS (SELECT 1 FROM product_variants pv"
            " JOIN product_stock ps ON ps.variant_id = pv.variant_id"
            " WHERE pv.product_id = p.product_id AND pv.color = %s"
            " AND ps.size = %s AND ps.stock > 0)"
        )
        params.extend((color, str(size)))
    elif color is not None:
        # 색은 variant다. 재고 조건을 명시하지 않으면 품절 색도 상품 상태로 보존한다.
        clauses.append(
            "EXISTS (SELECT 1 FROM product_variants pv"
            " WHERE pv.product_id = p.product_id AND pv.color = %s)"
        )
        params.append(color)
    if exclude_color is not None:
        # 상품 단위 검색이므로 제외 색 variant가 하나라도 있는 상품은 빼야
        # "검은색 말고" 요청에 검은색 옵션이 섞인 상품이 다시 나오지 않는다.
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM product_variants pv_ex"
            " WHERE pv_ex.product_id = p.product_id AND pv_ex.color = %s)"
        )
        params.append(exclude_color)
    if material is not None:
        clauses.append("p.material = %s")
        params.append(material)
    if exclude_material is not None:
        clauses.append("p.material IS DISTINCT FROM %s")
        params.append(exclude_material)
    if machine_washable is not None:
        clauses.append("p.machine_washable = %s")
        params.append(bool(machine_washable))     # 0/1 정수 -> boolean
    if size is not None and color is None:
        # 색을 지정하지 않았으면 어느 색이든 해당 사이즈 재고가 있으면 통과한다.
        clauses.append(
            "EXISTS (SELECT 1 FROM product_variants pv2"
            " JOIN product_stock ps ON ps.variant_id = pv2.variant_id"
            " WHERE pv2.product_id = p.product_id"
            " AND ps.size = %s AND ps.stock > 0)"
        )
        params.append(str(size))
    if in_stock is not None and size is None:
        # size가 있으면 이미 그 SKU의 stock>0을 확인했다. 색만 지정됐을 때는 같은
        # 색 variant의 재고를 보고, 색도 없으면 상품의 어느 SKU든 재고를 본다.
        stock_color = " AND pv3.color = %s" if color is not None else ""
        predicate = (
            "EXISTS" if in_stock else "NOT EXISTS"
        ) + " (SELECT 1 FROM product_variants pv3 JOIN product_stock ps3" \
            " ON ps3.variant_id=pv3.variant_id WHERE pv3.product_id=p.product_id" \
            f"{stock_color} AND ps3.stock > 0)"
        clauses.append(predicate)
        if color is not None:
            params.append(color)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def search_product_ids(conn, *, sort=None, limit=5, **filters):
    """구조화 조건만 SQL로 검색한다. 모든 값은 파라미터로 바인딩한다.

    sort 가 없으면 추천순(베이지안 평균)이다. 예전 기본값인 product_id 순은
    아마존 상품번호 순이라 품질과 무관했는데, 화면은 이를 "추천순"으로 표시했다.
    """
    where, params = _product_filter_where(**filters)
    # m 은 정수로 강제한 설정값이라 SQL 에 바로 넣어도 안전하다.
    # 전체 평균 C 는 비상관 서브쿼리라 한 번만 계산된다.
    m = int(config.RECOMMEND_PRIOR_REVIEWS)
    recommend = (
        f"(p.review_count * p.rating + {m} * (SELECT AVG(rating) FROM products))"
        f" / (p.review_count + {m}) DESC, p.review_count DESC, p.product_id ASC"
    )
    order_by = {
        "price_asc": "p.price ASC, p.product_id ASC",
        "price_desc": "p.price DESC, p.product_id ASC",
        "rating": "p.rating DESC, p.review_count DESC, p.product_id ASC",
        "review": "p.review_count DESC, p.rating DESC, p.product_id ASC",
    }.get(sort, recommend)
    rows = conn.execute(
        f"SELECT p.product_id FROM products p{where}"
        f" ORDER BY {order_by} LIMIT %s",
        (*params, int(limit)),
    )
    return [row[0] for row in rows]


def count_products(conn, **filters):
    """search_product_ids와 동일한 구조화 조건의 전체 상품 수."""
    where, params = _product_filter_where(**filters)
    return conn.execute(
        f"SELECT COUNT(*) FROM products p{where}", params
    ).fetchone()[0]


def _iso(value):
    """date -> 'YYYY-MM-DD'. store/shop.py 가 문자열로 비교하고 fromisoformat 으로 읽는다."""
    return None if value is None else value.isoformat()


def fetch_orders(conn, user_id=DEMO_USER_ID):
    """주문을 Store.orders 가 쓰는 모양의 dict 리스트로 돌려준다.

    날짜 열은 date 이지만 ISO 문자열로 돌려줍니다 — store/shop.py 의 정책 판단이
    문자열 비교와 date.fromisoformat() 위에 서 있기 때문입니다. 저장은 제대로 된
    타입으로, 경계에서 변환합니다.
    """
    orders = []
    for row in conn.execute(
        "SELECT order_id, variant_id, product_id, product_name, color, size,"
        " quantity, price, ordered_at, shipped_at, delivered_at, cancelled_at,"
        " returned_at, status, return_reason FROM orders WHERE user_id = %s"
        " ORDER BY ordered_at DESC, order_id",
        (user_id,),
    ):
        orders.append({
            "order_id": row["order_id"],
            "variant_id": row["variant_id"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "color": row["color"],
            "size": row["size"],
            "quantity": row["quantity"],
            "price": row["price"],
            "ordered_at": _iso(row["ordered_at"]),
            "shipped_at": _iso(row["shipped_at"]),
            "delivered_at": _iso(row["delivered_at"]),
            "cancelled_at": _iso(row["cancelled_at"]),
            "returned_at": _iso(row["returned_at"]),
            "status": row["status"],
            "return_reason": row["return_reason"],
        })
    return orders


def enum_values(conn):
    """Tool 스키마 enum에 쓸 값을 현재 DB에서 뽑는다."""
    def distinct(column, table="products"):
        return [r[0] for r in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} ORDER BY {column}"
        )]

    groups = [r[0] for r in conn.execute(
        "SELECT group_name FROM category_groups GROUP BY group_name"
        " ORDER BY MIN(sort_order)"
    )]
    return {
        "group": groups,
        "category": distinct("category"),
        "gender": distinct("gender"),
        "brand": distinct("brand"),
        "color": [r[0] for r in conn.execute(
            "SELECT DISTINCT color FROM product_variants ORDER BY color")],
        "material": distinct("material", "materials"),
        "status": [r[0] for r in conn.execute(
            "SELECT DISTINCT status FROM orders ORDER BY status"
        )],
    }


def category_group_map(conn):
    """DB에 저장된 대분류 → 소분류 매핑을 정의 순서대로 반환한다."""
    groups = {}
    for row in conn.execute(
        "SELECT group_name, category FROM category_groups ORDER BY sort_order"
    ):
        groups.setdefault(row["group_name"], []).append(row["category"])
    return groups


def read_catalog_metadata(dsn=None):
    """서버와 Tool이 사용할 카탈로그 메타데이터를 읽는다."""
    conn = connect(dsn)
    try:
        return {
            "enums": enum_values(conn),
            "category_groups": category_group_map(conn),
        }
    finally:
        conn.close()


def lock_cart_quantity(conn, user_id, variant_id, size):
    """장바구니 한 줄의 지금 수량을 잠그고 읽는다. 없으면 0. 트랜잭션 안에서 부른다."""
    row = conn.execute(
        "SELECT quantity FROM cart_items"
        " WHERE user_id = %s AND variant_id = %s AND size = %s FOR UPDATE",
        (user_id, variant_id, str(size)),
    ).fetchone()
    return row["quantity"] if row else 0


def fetch_cart(conn, products, user_id=DEMO_USER_ID):
    """장바구니를 store/shop.py 가 쓰는 모양으로 돌려준다.

    한 줄의 모양 — 색이 늘었다.
        {"product": <상품 dict 참조>, "color": "검은색",
         "variant_id": "P001-BLK", "size": 270, "quantity": 2}

    product 가 **참조**여야 합니다 — 상품 가격이 바뀌면 장바구니 금액도 같이
    따라가야 하므로 값을 복사하면 안 됩니다.

    ORDER BY seq 는 담은 순서를 지키기 위한 것입니다.
    """
    by_id = {p["id"]: p for p in products}
    cart = []
    for row in conn.execute(
        "SELECT c.variant_id, v.product_id, v.color, c.size, c.quantity"
        " FROM cart_items c JOIN product_variants v ON v.variant_id = c.variant_id"
        " WHERE c.user_id = %s ORDER BY c.seq",
        (user_id,),
    ):
        product = by_id.get(row["product_id"])
        if product is None:
            continue  # 카탈로그에서 사라진 상품. 조용히 건너뛴다
        cart.append({
            "product": product,
            "color": row["color"],
            "variant_id": row["variant_id"],
            "size": row["size"],
            "quantity": row["quantity"],
        })
    return cart


# --------------------------------------------------------------------------
# 데모 주문 — 상대 날짜 템플릿에서 복원
# --------------------------------------------------------------------------

def seed_demo_orders(conn, user_id=DEMO_USER_ID, *, picks=None, vary=False):
    """DB의 상대 날짜 템플릿으로 데모 주문을 복원한다.

    user_id 를 주면 그 사용자의 주문만 지우고 그 사용자 것으로 심는다.
    기본 사용자는 템플릿의 주문번호(ORD-1001…)를 그대로 쓰고, 다른 사용자는
    전체에서 유일한 번호를 새로 받는다.

    picks: 템플릿 중 쓸 것의 순번(1부터, seq 순). None 이면 전부.
    vary:  True 면 상품을 사용자마다 같은 분류의 다른 상품으로 바꾼다(상태·날짜는 템플릿 그대로).
           데모 사용자 다섯 명의 주문 내역이 똑같은 상품으로 겹치지 않게 한다 (seed_user_orders).
    """
    today = date.today()

    def when(days_ago):
        return None if days_ago is None else today - timedelta(days=days_ago)

    conn.execute("DELETE FROM orders WHERE user_id = %s", (user_id,))
    templates = conn.execute(
        "SELECT t.order_id, t.variant_id, t.size, t.quantity,"
        " t.ordered_days_ago, t.shipped_days_ago, t.delivered_days_ago,"
        " t.cancelled_days_ago, t.returned_days_ago, t.status, t.return_reason,"
        " v.product_id, v.color, p.name, p.price"
        " FROM demo_order_templates t"
        " JOIN product_variants v ON v.variant_id = t.variant_id"
        " JOIN products p ON p.product_id = v.product_id"
        " ORDER BY t.seq"
    ).fetchall()
    if picks is not None:
        wanted = set(picks)
        templates = [o for position, o in enumerate(templates, 1) if position in wanted]
    if vary:
        templates = [_vary_template(conn, dict(o), user_id, position)
                     for position, o in enumerate(templates, 1)]
    number = next_order_number(conn)
    taken = {r[0] for r in conn.execute("SELECT order_id FROM orders")}

    rows = []
    for o in templates:
        if user_id == DEMO_USER_ID and o["order_id"] not in taken:
            order_id = o["order_id"]
        else:
            order_id = f"ORD-{number}"
            number += 1
        taken.add(order_id)
        rows.append((
            order_id, user_id, o["variant_id"], o["product_id"], o["name"],
            o["color"], o["size"], o["quantity"], o["price"] * o["quantity"],
            when(o["ordered_days_ago"]), when(o["shipped_days_ago"]),
            when(o["delivered_days_ago"]), when(o["cancelled_days_ago"]),
            when(o["returned_days_ago"]), o["status"], o["return_reason"],
        ))
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO orders (order_id, user_id, variant_id, product_id,"
            " product_name, color, size, quantity, price, ordered_at, shipped_at,"
            " delivered_at, cancelled_at, returned_at, status, return_reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s)",
            rows,
        )
    conn.commit()
    return len(rows)


# 데모 사용자(config.DEMO_USERS, 기본 info1~info5)의 처음 주문 — 순서대로 한 명씩.
# 모두에게 같은 14건을 심으면 누구로 바꿔도 주문 내역이 똑같아 사용자를 나눈 의미가 없었다.
# 숫자는 demo_order_templates 의 순번(seq 순, 1부터). 템플릿 14건의 상태는
#   1~2 배송 준비 중 · 3~4 배송 중 · 5 취소됨 · 6~7 배송 완료(최근) · 8 반품 신청됨 ·
#   9~14 배송 완료(20~90일 전)
DEMO_ORDER_PLANS = [
    ("단골 — 모든 상태가 다 있다 (취소·반품·조회 시연)", None),
    ("진행 중 주문 위주 (배송 준비·배송 중 → 취소 시연)", (1, 2, 3, 4, 6)),
    ("최근 받은 주문 위주 (반품 시연)", (6, 7, 8, 9, 11)),
    ("가끔 사는 사용자", (5, 10, 14)),
    ("막 가입한 사용자 — 주문 없음", ()),
]


def seed_user_orders(conn, user_id):
    """사용자 종류에 맞는 처음 주문을 심는다. 새 사용자·처음부터 다시 가 이것을 쓴다.

    - 기본 사용자(demo): 템플릿 14건 그대로
    - 데모 사용자(info1~5): DEMO_ORDER_PLANS 의 자기 몫. 상품은 사람마다 다르게
    - 그 밖(브라우저가 처음 와서 생긴 익명 사용자): 주문 없음 — 빈 상태에서 시작한다
    """
    if user_id == DEMO_USER_ID:
        return seed_demo_orders(conn, user_id)
    users = list(config.DEMO_USERS)
    if user_id not in users:
        conn.execute("DELETE FROM orders WHERE user_id = %s", (user_id,))
        conn.commit()
        return 0
    _, picks = DEMO_ORDER_PLANS[users.index(user_id) % len(DEMO_ORDER_PLANS)]
    return seed_demo_orders(conn, user_id, picks=picks, vary=True)


def _vary_template(conn, template, user_id, position):
    """템플릿 주문의 상품을 같은 분류·같은 사이즈 재고가 있는 다른 상품으로 바꾼다.

    고르는 것은 (사용자, 순번)의 해시로 정해져 다시 심어도 같다. 후보가 없으면 템플릿 그대로.
    """
    candidates = conn.execute(
        "SELECT v.variant_id, v.product_id, v.color, p.name, p.price"
        " FROM demo_order_templates t"
        " JOIN product_variants tv ON tv.variant_id = t.variant_id"
        " JOIN products tp ON tp.product_id = tv.product_id"
        " JOIN products p ON p.category = tp.category"
        " JOIN product_variants v ON v.product_id = p.product_id"
        " JOIN product_stock s ON s.variant_id = v.variant_id AND s.size = t.size"
        " WHERE t.order_id = %s ORDER BY v.variant_id",
        (template["order_id"],)).fetchall()
    if not candidates:
        return template
    key = hashlib.sha256(f"{user_id}:{position}".encode()).digest()
    pick = candidates[int.from_bytes(key[:4], "big") % len(candidates)]
    template.update(variant_id=pick["variant_id"], product_id=pick["product_id"],
                    color=pick["color"], name=pick["name"], price=pick["price"])
    return template


def reset_user(conn, user_id):
    """한 사용자의 장바구니·주문·선호를 처음 상태로 되돌린다.

    상품과 재고는 사용자들이 함께 보는 카탈로그라 건드리지 않는다.
    에이전트 상태는 세션 DB 에 있으므로 여기서 지우지 않는다 (session_state.delete_agent_state).
    """
    ensure_user(conn, user_id)
    conn.execute("DELETE FROM cart_items WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM wishlists WHERE user_id = %s", (user_id,))
    conn.execute("DELETE FROM user_preferences WHERE user_id = %s", (user_id,))
    return seed_user_orders(conn, user_id)


# --------------------------------------------------------------------------
# DB 준비 확인
# --------------------------------------------------------------------------

def looks_initialised(dsn=None):
    """카탈로그가 들어 있는 DB 인가 — products 테이블에 행이 있는가.

    DB 가 없거나 접속할 수 없어도 False 다.
    """
    try:
        with psycopg.connect(dsn or SHOP_DSN) as conn:
            return conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] > 0
    except psycopg.Error:
        return False
