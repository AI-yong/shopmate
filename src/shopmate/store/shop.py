"""쇼핑몰 도메인 계층 — 이 프로젝트의 심장.

이 파일의 역할은 딱 하나입니다.

    "쇼핑몰의 상태와 규칙을 아는 유일한 곳"

  - 상태: 상품 목록, 장바구니, 주문 내역
  - 규칙: 재고 판정, 취소 가능 여부, 반품 가능 기간

agent/tools.py 도 server.py 도 정책 판단을 직접 하지 않고 여기에 물어봅니다.
그래야 "can_cancel 은 된다고 했는데 cancel_order 는 거부하는" 모순이 생기지 않습니다.
Tool 이 늘어나도 규칙은 여기 한 곳만 고치면 됩니다.

이 파일은 웹도 LLM 도 모릅니다. 저장은 store/db.py 에 맡깁니다.
"""

from datetime import date, timedelta
from typing import NamedTuple

import sys
import threading

from shopmate import config
from shopmate.store import db
from shopmate.store import events


# --- 정책 상수 -------------------------------------------------------------
# 규칙에 해당하는 숫자는 코드 안에 흩뿌리지 말고 여기 모아둡니다.
# "반품 기간을 14일에서 30일로 바꿔줘" 같은 요구가 오면 이 줄만 고치면 됩니다.

RETURN_PERIOD_DAYS = 14


# 주문 상태. 문자열을 코드 여기저기에 직접 쓰면 오타를 잡을 수 없으므로 상수로 둡니다.
STATUS_PREPARING = "배송 준비 중"
STATUS_CANCELLED = "취소됨"
STATUS_RETURN_REQUESTED = "반품 신청됨"


class _StockConflict(Exception):
    """checkout 트랜잭션 안에서 재고 관문이 거절했음을 알린다.

    사전 확인은 통과했는데 조건부 UPDATE 가 거절한 경우입니다.
    그 사이에 재고가 빠져나갔다는 뜻이고, 롤백해야 합니다.
    """

    def __init__(self, name, size, quantity):
        super().__init__(f"{name} {size} {quantity}")
        self.name = name
        self.size = size
        self.quantity = quantity


class _CartChanged(Exception):
    """checkout 트랜잭션 안에서 본 DB 장바구니가 메모리의 장바구니보다 적다.

    같은 사용자의 다른 Store(서버 프로세스 둘, 다른 탭)가 먼저 결제했거나 수량을
    줄였다는 뜻이다. 메모리만 믿고 결제하면 같은 장바구니가 두 번 주문된다.
    """


class Decision(NamedTuple):
    """정책 판정 결과.

    can_cancel / can_return 처럼 "되나요?"를 묻는 메서드가 돌려주는 형태입니다.

    단순히 True/False 만 돌려주면 안 되는 이유가 있습니다. 에이전트가
    "배송이 이미 시작돼서 취소가 어렵습니다. 대신 수령 후 반품하실 수 있어요"
    라고 답하려면 '왜 안 되는지'와 '그럼 어떻게 해야 하는지'를 알아야 합니다.

    allowed     : 가능 여부
    reason      : 그렇게 판정한 이유 (사용자에게 그대로 보여줘도 되는 문장)
    alternative : 불가능할 때 안내할 대안. 가능하면 None.
    """

    allowed: bool
    reason: str
    alternative: str | None = None


def _josa(word, pair="은는"):
    """받침에 맞는 조사를 고른다. "어반 러너 은" 같은 문장을 막는다.

    pair 는 "은는" · "이가" · "을를" 처럼 (받침 있음, 받침 없음) 순서다.
    숫자·영문으로 끝나면 읽는 소리를 기준으로 본다.
    """
    if not word:
        return pair[1]
    last = word.strip()[-1]
    if "가" <= last <= "힣":
        has_batchim = (ord(last) - 0xAC00) % 28 != 0
    elif last.isdigit():
        has_batchim = last in "0136780"        # 영·일·삼·육·칠·팔·십
    elif last.isalpha():
        has_batchim = last.lower() in "lmnr"   # 엘·엠·엔·알
    else:
        has_batchim = False
    return pair[0] if has_batchim else pair[1]


def _positive_int(value, label):
    """수량 값을 검사한다. 통과하면 int, 아니면 오류 메시지 문자열을 돌려준다.

    agent/tools.py 가 이미 스키마로 검사하지만 여기서도 봅니다.
    store 는 Tool 말고 화면(server.py)에서도 불리고, 나중에 다른 진입점이 생길 수도
    있습니다. 상태를 바꾸는 함수는 자기 입력을 스스로 지켜야 합니다.
    (음수 수량으로 장바구니가 1개에서 6개로 늘어난 적이 있습니다)
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{label}은 정수여야 합니다. 받은 값: {value!r}"
    if value < 1:
        return f"{label}은 1 이상이어야 합니다. 받은 값: {value}"
    return value




def _db_file(conn):
    """이 연결이 붙어 있는 DB(`host/dbname`). 오류 메시지와 벡터 캐시 키에 쓴다."""
    try:
        info = conn.info
        return f"{info.host or 'local'}/{info.dbname}"
    except Exception:
        return ""


class Store:
    """쇼핑몰 하나를 표현하는 객체.

    상태를 인스턴스 안에 들고 있으므로 세션마다 하나씩 만들면 서로 간섭하지 않습니다.
    서버는 사용자마다 Store 를 하나씩 둡니다 (server.Session).

    데이터는 PostgreSQL 업무 DB 에서 읽습니다 (store/db.py).

    캐시의 경계가 하나 있습니다. 이름·가격·색상처럼 실행 중에 바뀌지 않는 값은
    메모리에 올려도 낡지 않지만, 재고처럼 바뀌는 값은 캐시하면 낡습니다.
    그래서 재고는 DB 가 판정하고(take_stock), 메모리의 재고는 그 결과를 따라가는
    캐시입니다(_sync_stock).
    """

    def __init__(self, db_path=None, user_id=None):
        # db_path 는 업무 DB 의 DSN 이다. 주지 않으면 config.SHOP_DSN.
        # 이 Store 가 누구의 장바구니·주문을 보는가. 서버는 브라우저마다 다른 값을
        # 넘기고, 터미널은 기본 사용자로 동작한다.
        self.user_id = user_id or db.DEMO_USER_ID
        # 이 Store 의 DB 연결(self.conn)을 한 번에 한 쪽만 쓰게 하는 잠금. 서버에서는 화면 요청과
        # 에이전트 턴(Toolbox.call)이 같은 Store 를 쓴다. 한쪽이 여러 문장을 한 트랜잭션으로 묶는
        # 동안(결제·취소) 다른 쪽의 commit 이 끼어들면 그 트랜잭션이 중간에 확정된다.
        # RLock 인 이유: 쥔 쪽이 안에서 다시 Store 를 불러도 스스로 막히지 않게.
        self.db_lock = threading.RLock()
        self.conn = db.connect(db_path or db.SHOP_DSN)
        db.ensure_user(self.conn, self.user_id)

        self.products = db.fetch_products(self.conn)
        self._category_groups = db.category_group_map(self.conn)
        self.orders = db.fetch_orders(self.conn, self.user_id)
        self.cart = db.fetch_cart(self.conn, self.products, self.user_id)

    @property
    def today(self):
        """오늘 날짜. 서버가 자정을 넘겨 돌아도 날짜가 굳지 않게 매번 새로 읽는다."""
        return date.today()

    # ==================================================================
    # 영속
    #
    # 재고는 DB 가 판정합니다. 메모리는 그 결과를 따라가는 캐시입니다.
    # "확인하고 나서 차감" 은 그 사이가 열려 있습니다(TOCTOU). 여기서는
    # 조건을 UPDATE 안에 넣어 한 문장으로 만듭니다.
    # ==================================================================

    def _size_key(self, size):
        """DB 카탈로그의 FREE/S/M/235를 하나의 문자열 키로 맞춘다."""
        if size is not None:
            return str(size).strip().upper()
        return size

    def _persist_cart_line(self, variant_id, size, quantity, *, commit=True):
        """장바구니 한 줄을 DB 에 맞춘다. quantity 가 0 이하면 지운다.

        키가 (사용자, **색**, 사이즈) 다. 같은 상품이라도 색이 다르면 다른 줄이다.
        """
        if quantity <= 0:
            self.conn.execute(
                "DELETE FROM cart_items"
                " WHERE user_id = %s AND variant_id = %s AND size = %s",
                (self.user_id, variant_id, size),
            )
        else:
            # ON CONFLICT 가 seq 를 유지하므로 줄의 순서가 보존됩니다.
            # "두 번째 거 담아줘" 가 가리키는 순서와 화면 순서가 같아야 합니다.
            self.conn.execute(
                "INSERT INTO cart_items (user_id, variant_id, size, quantity)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (user_id, variant_id, size)"
                " DO UPDATE SET quantity = excluded.quantity",
                (self.user_id, variant_id, size, quantity),
            )
        events.enqueue(
            self.conn, "cart_updated", "cart", self.user_id,
            {"user_id": self.user_id, "variant_id": variant_id,
             "size": str(size), "quantity": quantity, "source": "runtime"},
        )
        if commit:
            self.conn.commit()

    def _sync_stock(self, product_id):
        """DB 의 재고를 메모리 상품 dict 로 옮긴다. DB 가 주인이다.

        색별 재고와 합계를 함께 되살린다 — 둘 중 하나만 갱신하면 어긋난다.
        """
        product = self.get_product(product_id)
        if product is None:
            return
        for info in product["colors"].values():
            info["sizes"] = {}
        totals = {}
        for row in self.conn.execute(
            "SELECT v.color, s.size, s.stock FROM product_stock s"
            " JOIN product_variants v ON v.variant_id = s.variant_id"
            " WHERE v.product_id = %s ORDER BY v.sort_order, s.size",
            (product_id,),
        ):
            info = product["colors"].get(row["color"])
            if info is not None:
                info["sizes"][row["size"]] = row["stock"]
            totals[row["size"]] = totals.get(row["size"], 0) + row["stock"]
        product["sizes"] = dict(sorted(totals.items()))

    def _persist_new_order(self, order):
        """새 주문을 DB 에 넣는다. 커밋은 호출자가 한다 (트랜잭션 안이므로)."""
        self.conn.execute(
            "INSERT INTO orders (order_id, user_id, variant_id, product_id,"
            " product_name, color, size, quantity, price, ordered_at, shipped_at,"
            " delivered_at, cancelled_at, returned_at, status, return_reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s)",
            (
                order["order_id"], self.user_id, order["variant_id"],
                order["product_id"], order["product_name"], order["color"],
                order["size"], order["quantity"], order["price"],
                order["ordered_at"], order.get("shipped_at"),
                order.get("delivered_at"), order.get("cancelled_at"),
                order.get("returned_at"), order["status"],
                order.get("return_reason"),
            ),
        )

    def _persist_order_state(self, order, expected_status):
        """취소·반품으로 바뀐 주문 상태를 DB 에 반영한다. 반영했으면 True.

        can_cancel·can_return 은 이 Store 가 메모리에 든 주문으로 판단한다.
        같은 사용자의 Store 가 둘이면(서버 프로세스 둘, 세션 재생성) 둘 다
        "취소 가능" 으로 보고 두 번 취소해 재고를 두 번 돌려줄 수 있다.
        그래서 DB 의 상태가 판단 당시 그대로일 때만 바꾼다 — 판단과 쓰기 사이에
        누가 먼저 바꿨으면 0행이 되고, 호출부가 롤백한다.
        """
        # user_id 조건: 소유권은 self.orders(이 사용자 것만 읽어 온 목록)로도 걸러지지만,
        # 메모리 목록 하나에만 기대지 않고 DB 문장 자체가 남의 주문을 못 바꾸게 한다.
        cur = self.conn.execute(
            "UPDATE orders SET status = %s, cancelled_at = %s, returned_at = %s,"
            " return_reason = %s WHERE order_id = %s AND user_id = %s AND status = %s",
            (
                order["status"], order.get("cancelled_at"),
                order.get("returned_at"), order.get("return_reason"),
                order["order_id"], self.user_id, expected_status,
            ),
        )
        return cur.rowcount == 1

    def _reload_order(self, order_id):
        """메모리의 주문 하나를 DB 값으로 되돌린다 (다른 Store 가 먼저 바꿨을 때)."""
        order = self.get_order(order_id)
        if order is None:
            return
        for fresh in db.fetch_orders(self.conn, self.user_id):
            if fresh["order_id"] == order_id:
                order.update(fresh)
                return

    # ==================================================================
    # 상품 조회
    # ==================================================================

    def search_products(
        self,
        product_name=None,
        group=None,
        category=None,
        gender=None,
        brand=None,
        max_price=None,
        min_price=None,
        color=None,
        size=None,
        material=None,
        machine_washable=None,
        in_stock=None,
        sort=None,
        limit=5,
        exclude_category=None,
        exclude_color=None,
        exclude_material=None,
    ):
        """조건에 맞는 상품 목록을 반환한다.

        color 와 category 는 **완전 일치**로 비교합니다.
        "블랙" 을 "검은색" 으로 바꾸는 일은 여기서 하지 않습니다.
        그건 모델의 몫이고, agent/tools.py 스키마의 enum 이 유효한 값을 알려줍니다.
        (자세한 이유는 아래 '왜 별칭 표를 쓰지 않는가' 참고)

        gender 는 "공용" 상품도 함께 반환합니다.
        brand, material, machine_washable, sort 로도 걸러낼 수 있습니다.

        material 은 "면", "린넨", "캐시미어" 같은 짧은 이름입니다.
        혼용률("면 100%")은 material_detail 에 따로 있습니다.
        검색·비교에는 짧은 이름을 쓰고 사람에게 보여줄 때 혼용률을 씁니다.

        1) 자연어(용도·기능·착용감)는 여기서 다루지 않습니다. 그것은
           search_semantic_result(query, ...) 가 임베딩으로 처리합니다.
           예전의 keyword 부분일치는 자연어 질의 58개 중 56개에 결과를 내지
           못해 제거했습니다. 이 함수는 **구조화 조건만**
           거르는 SQL 필터층입니다.

        2) size 가 주어지면 그 사이즈 재고가 1개 이상인 상품만 남깁니다.

        3) 가격은 min_price <= price <= max_price.

        반환: 상품 dict 의 리스트. 최대 limit 개.
              찾은 게 없으면 빈 리스트 (None 이 아니라).

        ----------------------------------------------------------------
        왜 별칭 표를 쓰지 않는가

        "블랙 -> 검은색" 같은 표를 만들면 아무리 채워도 빠진 게 나옵니다.
        검정, black, 까만색, 다크... 끝이 없습니다.
        자연어의 흔들림을 흡수하는 건 모델이 할 일이지 규칙이 할 일이 아닙니다.

        대신 두 가지로 처리합니다.

          (1) agent/tools.py 스키마의 enum 이 유효한 값을 못박습니다.
              모델이 "블랙"을 듣고도 enum 에 있는 "검은색"을 넣습니다.

          (2) 그래도 빗나가면 Tool 이 실패 메시지에 유효한 값을 실어 보냅니다.
              "'다크' 색상은 없습니다. 가능한 색상: 검은색, 흰색, ..."
              모델이 이걸 읽고 스스로 다시 검색합니다.

        (2) 가 별칭 표보다 나은 이유: 별칭 표는 제가 미리 상상한 오타만 막지만,
        이 방식은 색상이든 카테고리든 사이즈든 모든 어긋남을 처리합니다.

        규칙과 모델의 경계는 이렇게 잡습니다.
          - 규칙: 틀리면 안 되고 검증 가능한 것 (재고 수량, 반품 기간, 배송 상태)
          - 모델: 언어가 지저분한 지점 ("블랙", "저렴한 거", "어제 주문한 운동화")
        ----------------------------------------------------------------
        """
        filters = dict(
            product_name=product_name, group=group, category=category,
            gender=gender, brand=brand,
            max_price=max_price, min_price=min_price, color=color, size=size,
            material=material, machine_washable=machine_washable,
            in_stock=in_stock,
            exclude_category=exclude_category, exclude_color=exclude_color,
            exclude_material=exclude_material,
        )
        ids = db.search_product_ids(
            self.conn, sort=sort, limit=limit, **filters)
        by_id = {product["id"]: product for product in self.products}
        return [by_id[pid] for pid in ids if pid in by_id]

    # ==================================================================
    # 의미 검색 — 순위 층
    #
    # 필터(2층)가 후보를 좁힌 뒤, 그 후보의 순서를 정하는 층입니다.
    # SQL 필터는 전용 인자가 없는 말("오피스")로는 순서를 매기지 못하므로,
    # 그 말과 상품 설명의 임베딩 유사도로 순서를 정합니다.
    #
    # 임베딩이 없으면(인덱싱 전) available=False 를 돌려주고, 호출자는 SQL 결과로 되돌아갑니다.
    # ==================================================================

    # {host/dbname: (ids, matrix, index, backend)} — 같은 DB 를 보는 Store 들이 공유
    _VEC_SHARED = {}

    def _ensure_vectors(self):
        """임베딩 행렬을 한 번만 읽어 인스턴스에 붙인다."""
        if getattr(self, "_vec_ready", False):
            return self._vec_ids is not None
        self._vec_ready = True
        self._vec_ids = None
        self._vec_matrix = None
        self._vec_index = {}
        self._query_backend = None

        # 0) 같은 DB 를 보는 Store 끼리는 행렬과 질의 모델을 공유한다.
        #    서버가 사용자마다 Store 를 하나씩 두므로, 없으면 사용자 수만큼
        #    문서 행렬과 sentence-transformers 모델(수백 MB)이 올라간다.
        #    임베딩이 바뀌면 서버를 재시작하니 캐시 무효화는 프로세스 수명과 같다.
        cache_key = _db_file(self.conn)
        shared = Store._VEC_SHARED.get(cache_key) if cache_key else None
        if shared is not None:
            self._vec_ids, self._vec_matrix, self._vec_index, self._query_backend = shared
            return True

        # 1) 문서 벡터를 읽는다. 없으면 의미 검색을 끈다.
        #    (아직 상품 벡터를 만들지 않은 상태 — 정상적인 경우다)
        try:
            from shopmate.search import text_embedding
            ids, matrix = text_embedding.load_matrix(self.conn)
        except Exception as exc:
            print(f"[검색] 문서 벡터를 읽지 못했습니다 — 의미 검색을 끕니다: {exc}",
                  file=sys.stderr)
            self._vec_ids = None
            return False
        if ids is None:
            return False

        # 2) 질의 백엔드를 만든다. 여기 실패는 "환경이 없다" 는 뜻이다
        #    (sentence-transformers 미설치, 모델 파일 없음). 앱은 살려두되
        #    조용히 넘기지 않는다 — 예전에 이 except 가 소리 없이 의미 검색을
        #    통째로 끄는 바람에, 검색 결과가 이상해진 뒤에야 알아챘습니다.
        try:
            backend = text_embedding.query_backend(self.conn)
        except Exception as exc:
            print(f"[검색] 질의 백엔드를 만들지 못했습니다 — 의미 검색을 끕니다: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            self._vec_ids = None
            return False

        # 3) 문서 벡터와 질의 벡터가 같은 공간인지 본다.
        #
        #    여기가 어긋나는 건 "환경이 없다" 가 아니라 **데이터가 오염됐다**
        #    는 뜻입니다(문서 벡터만 바뀌고 meta 기록이 그대로인 경우).
        #
        #    이건 끄고 넘어가면 안 됩니다. 끄면 검색이 "그냥 좀 안 맞는"
        #    것처럼 보이는데, 실은 의미 검색이 없는 겁니다. 멈추고 말합니다.
        doc_dim = matrix.shape[1]
        query_dim = getattr(backend, "dim", None)
        if query_dim is None:
            try:
                query_dim = int(backend.encode(["차원확인"])[0].shape[0])
            except Exception as exc:
                print(f"[검색] 질의 인코딩에 실패했습니다 — 의미 검색을 끕니다: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                self._vec_ids = None
                return False
        if int(query_dim) != int(doc_dim):
            recorded = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
            raise RuntimeError(
                "임베딩 DB 가 어긋났습니다 — 문서 벡터와 질의 모델이 다릅니다.\n"
                f"  이 DB      {_db_file(self.conn)}\n"
                f"  문서 벡터   {doc_dim}차원 x {len(ids):,}개\n"
                f"  질의 모델   {recorded[0] if recorded else '(기록 없음)'}"
                f" -> {query_dim}차원\n"
                "  상품 벡터를 지금 질의 모델(EMBED_MODEL_NAME)로 다시 만들어야 합니다.")

        self._vec_ids = ids
        self._vec_matrix = matrix
        self._vec_index = {pid: i for i, pid in enumerate(ids)}
        self._query_backend = backend
        if cache_key:
            Store._VEC_SHARED[cache_key] = (
                self._vec_ids, self._vec_matrix, self._vec_index, self._query_backend)
        return True

    def embed_query(self, query):
        """질의를 벡터로. 임베딩이 없으면 None."""
        if not self._ensure_vectors() or not query:
            return None
        return self._query_backend.encode([query])[0]

    def semantic_scores(self, query, products):
        """{product_id: 유사도}. 임베딩이 없으면 빈 dict."""
        vector = self.embed_query(query)
        if vector is None or not products:
            return {}
        rows, ids = [], []
        for product in products:
            i = self._vec_index.get(product["id"])
            if i is not None:
                rows.append(i)
                ids.append(product["id"])
        if not rows:
            return {}
        sims = self._vec_matrix[rows] @ vector
        return {pid: float(s) for pid, s in zip(ids, sims)}

    def search_semantic_result(self, query, top_k=20, min_score=None,
                               min_results=0, **filters):
        """필터 후 의미 점수 하한을 적용한 검색 결과와 상태를 함께 반환한다.

        post-filter(유사도 top-k 먼저, 필터 나중)로 하면 "15만원 이하 검은색"
        인데 top-k 안에 조건 맞는 게 없어서 결과가 비는 일이 생깁니다.

        available=False와 products=[]는 임베딩을 사용할 수 없다는 뜻이고,
        available=True와 products=[]는 정상 검색했지만 기준 통과 상품이 없다는 뜻이다.

        min_results 는 최소 표시 개수다. 기준을 통과한 상품이 그보다 적으면 기준
        아래에서 점수 순으로 채운다. 그렇게 더한 개수가 backfilled 로 돌아가므로
        호출자는 "딱 맞는 것이 적어 비슷한 상품을 함께 보여준다" 고 알릴 수 있다.
        통과가 충분하면(대부분의 질의) 아무 일도 하지 않는다.
        """
        filters.pop("query", None)
        filters.pop("sort", None)
        filters.pop("limit", None)
        candidates = self.search_products(limit=10 ** 9, **filters)
        scores = self.semantic_scores(query, candidates)
        if not scores:
            return {
                "available": False, "products": [], "qualified_count": 0,
                "backfilled": 0,
            }
        ranked = sorted(
            (product for product in candidates
             if min_score is None or scores.get(product["id"], -1.0) >= min_score),
            key=lambda p: (-scores[p["id"]], p["id"]),
        )
        selected = ranked[:top_k]

        # 기준 통과가 너무 적으면 기준 아래에서 점수 순으로 채운다.
        backfilled = 0
        if min_results and len(selected) < min_results:
            chosen = {p["id"] for p in selected}
            rest = sorted(
                (p for p in candidates if p["id"] not in chosen and p["id"] in scores),
                key=lambda p: (-scores[p["id"]], p["id"]),
            )
            extra = rest[:min(min_results, top_k) - len(selected)]
            selected = selected + extra
            backfilled = len(extra)

        return {
            "available": True,
            "products": selected,
            "qualified_count": len(ranked),
            "backfilled": backfilled,
        }

    def recommend_key(self):
        """추천순 점수 함수. 값이 클수록 앞이다.

        베이지안 평균 (v·R + m·C) / (v + m). v 는 리뷰 수, R 은 평점, C 는 카탈로그
        전체 평균 평점, m 은 config.RECOMMEND_PRIOR_REVIEWS 다. 리뷰가 적은 상품의
        평점은 C 쪽으로 끌려간다. db.search_product_ids 의 SQL 과 같은 식이다.
        """
        m = config.RECOMMEND_PRIOR_REVIEWS
        ratings = [p["rating"] for p in self.products]
        prior = sum(ratings) / len(ratings) if ratings else 0.0
        return lambda p: (p["review_count"] * p["rating"] + m * prior) / (p["review_count"] + m)

    def sort_products(self, products, sort):
        """이미 고른 후보 집합을 서비스 정렬 기준으로 안정적으로 정렬한다."""
        rows = list(products)
        if sort == "recommend":
            score = self.recommend_key()
            return sorted(rows, key=lambda p: (-score(p), -p["review_count"], p["id"]))
        keys = {
            "price_asc": lambda p: (p["price"], p["id"]),
            "price_desc": lambda p: (-p["price"], p["id"]),
            "rating": lambda p: (-p["rating"], -p["review_count"], p["id"]),
            "review": lambda p: (-p["review_count"], -p["rating"], p["id"]),
        }
        key = keys.get(sort)
        return sorted(rows, key=key) if key else rows

    def get_product(self, product_id):
        """상품 ID 로 상품 하나를 찾는다. 없으면 None.

        sizes 를 포함한 전체 dict 를 그대로 돌려줍니다.
        빼고 주면 "270 사이즈 재고 있어?" 에 답할 수 없습니다.
        """
        for product in self.products:
            if product["id"] == product_id:
                return product
        return None

    def get_stock(self, product_id, size, color=None):
        """재고 수량. 상품·사이즈·색이 없으면 0.

        color 를 주지 않으면 **모든 색을 합친** 수량이다. "이 상품 270 있나" 는
        색을 안 따지므로 그게 맞다. 실제로 담을 때는 색을 지정해야 한다 —
        합계가 3개여도 그게 세 색에 하나씩이면 한 색으로는 못 산다.
        """
        size = self._size_key(size)
        product = self.get_product(product_id)
        if product is None:
            return 0
        if color is None:
            return product["sizes"].get(size, 0)
        info = product["colors"].get(color)
        return info["sizes"].get(size, 0) if info else 0

    def colors_in_stock(self, product_id, size):
        """그 사이즈를 살 수 있는 색 목록. 품절 안내에서 대안을 제시할 때 쓴다."""
        size = self._size_key(size)
        product = self.get_product(product_id)
        if product is None:
            return []
        return [c for c, info in product["colors"].items()
                if info["sizes"].get(size, 0) > 0]

    def in_stock_first(self, rows, key="id"):
        """품절(모든 색·사이즈 재고 0) 상품을 맨 뒤로 보낸다. 빼지 않고, 나머지 순서는 그대로다.

        rows 는 상품 dict(key="id") 이거나 Tool 결과 행(key="product_id") 이다.
        카탈로그에 없는 ID 는 품절로 보지 않는다(판단할 근거가 없다).
        """
        rows = list(rows)
        wanted = {row.get(key) for row in rows}
        sold_out = {product["id"] for product in self.products
                    if product["id"] in wanted
                    and not any(q > 0 for q in product["sizes"].values())}
        if not sold_out:
            return rows
        return ([row for row in rows if row.get(key) not in sold_out]
                + [row for row in rows if row.get(key) in sold_out])

    def colors_in_stock_any(self, product_id):
        """어느 사이즈든 재고가 있는 색 목록. "그 색은 전 사이즈 품절" 때 대안용."""
        product = self.get_product(product_id)
        if product is None:
            return []
        return [c for c, info in product["colors"].items()
                if any(q > 0 for q in info["sizes"].values())]

    # ------------------------------------------------------------------
    # 아래 두 메서드는 "모델이 잘못된 값을 넣었을 때 스스로 고칠 수 있게"
    # 유효한 값을 알려주기 위한 것입니다. 별칭 표를 대신합니다.
    # agent/tools.py 의 실패 메시지에서 사용합니다.
    # ------------------------------------------------------------------

    def available_colors(self):
        """데이터에 실제로 존재하는 색상 목록.

        set 으로 중복을 없애고 sorted 로 순서를 고정합니다.
        순서를 고정하는 이유: 매번 다른 순서로 나가면 모델 응답도 흔들리고,
        프롬프트 캐시도 매번 깨집니다.
        """
        return sorted({c for product in self.products for c in product["colors"]})

    def available_categories(self):
        """데이터에 실제로 존재하는 카테고리 목록."""
        return sorted({product["category"] for product in self.products})

    def count_products(self, **filters):
        """조건에 맞는 상품이 모두 몇 개인지 센다.

        search_products 는 limit 으로 잘라서 돌려주기 때문에,
        모델이 "이게 전부인지 일부인지" 를 알 수가 없습니다.
        그러면 "운동화는 5종류 있습니다" 처럼 잘못 답하게 됩니다.
        """
        filters.pop("limit", None)
        filters.pop("sort", None)
        return db.count_products(self.conn, **filters)

    def find_product_by_name(self, name):
        """상품명으로 상품을 찾는다. 정확히 일치하는 것을 먼저, 없으면 부분 일치.

        모델이 상품 ID 대신 이름을 넘기는 일이 흔합니다.
        ("시티 라이트 재킷 세탁기 돌려도 돼?" 처럼 사용자가 이름으로 말하니까요)
        그때마다 검색을 다시 시키는 대신 이름으로도 찾을 수 있게 열어둡니다.

        반환: (상품 또는 None, 후보 리스트)
              후보가 여럿이면 상품은 None 이고 후보 목록이 채워집니다.
        """
        if not name:
            return None, []

        needle = name.strip().lower()

        # 이름은 더 이상 유일하지 않다. 아마존 카탈로그 14,595개 중 37% 가 다른 상품과
        # 이름이 같다 ("그래픽 프린트 반팔 티셔츠" 82개). 정확히 일치해도 여럿이면
        # 첫 번째를 돌려주면 안 된다 — 아무거나 하나가 소리 없이 담긴다. 후보로 넘겨
        # 에이전트가 되묻게 한다.
        exact = [p for p in self.products if p["name"].lower() == needle]
        if len(exact) == 1:
            return exact[0], []
        if exact:
            return None, exact

        matches = [p for p in self.products if needle in p["name"].lower()]
        if len(matches) == 1:
            return matches[0], []
        return None, matches

    def available_groups(self):
        """카테고리 대분류 목록. 정의된 순서를 그대로 유지한다."""
        return list(self._category_groups.keys())

    def categories_in_group(self, group):
        """대분류에 속한 소분류 목록. 실제로 상품이 있는 것만 돌려준다."""
        existing = set(self.available_categories())
        return [c for c in self._category_groups.get(group, []) if c in existing]

    def group_of(self, category):
        """소분류가 어느 대분류에 속하는지."""
        for group, categories in self._category_groups.items():
            if category in categories:
                return group
        return None

    def available_brands(self):
        """브랜드 목록."""
        return sorted({product["brand"] for product in self.products
                       if product["brand"] != "브랜드 미상"})

    @staticmethod
    def _brand_key(value):
        """대소문자·공백·구두점을 무시하되 추측 매칭은 하지 않는 브랜드 키."""
        return "".join(character.casefold() for character in str(value or "")
                       if character.isalnum())

    def resolve_brand(self, value):
        """사용자 표기를 DB의 실제 브랜드 표기로 정확히 정규화한다."""
        key = self._brand_key(value)
        if not key:
            return None
        matches = [brand for brand in self.available_brands()
                   if self._brand_key(brand) == key]
        return matches[0] if len(matches) == 1 else None

    def available_sizes(self, category=None, gender=None):
        """해당 조건에서 실제로 존재하는 사이즈 목록.

        카테고리마다 사이즈 체계가 달라서(신발 220~290 / 상의 44~110 / 팬츠 25~36)
        모델이 엉뚱한 사이즈를 넣기 쉽습니다. 그때 이 목록을 알려주면 스스로 고칩니다.
        tools 의 상품 검색이 "결과 0건 + size 지정" 일 때 이걸 부릅니다.
        """
        sizes = set()
        for product in self.products:
            if category is not None and product["category"] != category:
                continue
            if gender is not None and product["gender"] not in (gender, "공용"):
                continue
            sizes.update(product["sizes"].keys())
        return sorted(sizes, key=db._size_sort_key)

    # ==================================================================
    # 장바구니
    # ==================================================================

    def add_to_cart(self, product_id, size, color=None, quantity=1):
        """장바구니에 상품을 담는다.

        **색을 골라야 담긴다.** 상품에 색이 여럿이면 color 없이는 담을 수 없고,
        어느 색이 있는지 알려주며 거절한다. 에이전트가 그 문장을 읽고 되묻는다.
        색이 하나뿐인 상품은 물을 것이 없으므로 그냥 담는다.

        불변조건: (상품, 색, 사이즈) 조합당 장바구니에 한 줄만 존재한다.
        같은 조합을 다시 담으면 줄을 늘리지 않고 수량만 올린다.
        remove_from_cart 가 이 가정 위에서 동작하므로 반드시 지켜야 한다.

        장바구니 한 줄의 모양:
            {"product": <상품 dict 참조>, "color": "검은색",
             "variant_id": "P001-BLK", "size": 270, "quantity": 2}

        반환: (bool, str)
        """
        size = self._size_key(size)
        checked = _positive_int(quantity, "수량")
        if isinstance(checked, str):
            return False, checked
        quantity = checked

        product = self.get_product(product_id)
        if product is None:
            return False, f"'{product_id}' 상품을 찾을 수 없습니다."

        colors = product["colors"]
        if not colors:
            return False, (f"{product['name']}{_josa(product['name'])} 지금 "
                           f"살 수 있는 색상이 없습니다.")

        # --- 색 고르기 ---
        if color is None:
            if len(colors) == 1:
                color = next(iter(colors))     # 하나뿐이면 물을 것이 없다
            else:
                return False, (
                    f"{product['name']}{_josa(product['name'])} 색상을 골라야 "
                    f"담을 수 있습니다. 가능한 색상: {', '.join(colors)}"
                )
        if color not in colors:
            return False, (
                f"{product['name']}에 '{color}' 색상은 없습니다. "
                f"가능한 색상: {', '.join(colors)}"
            )

        # --- 사이즈·재고 ---
        sizes = colors[color]["sizes"]
        if size not in sizes:
            available = ", ".join(str(s) for s in sizes)
            return False, (
                f"{product['name']} {color}에는 {size} 사이즈가 없습니다. "
                f"가능한 사이즈: {available}"
            )

        stock = sizes.get(size, 0)
        if stock < quantity:
            # 다른 색에 그 사이즈가 있으면 알려 준다. 모델이 이 문장을 읽고
            # 다음 행동을 정하므로, 막다른 길로 두지 않는다.
            alternatives = [c for c in self.colors_in_stock(product_id, size)
                            if c != color]
            hint = (f" {', '.join(alternatives)} 색상에는 재고가 있습니다."
                    if alternatives else "")
            if stock == 0:
                return False, (
                    f"{product['name']} {color} {size} 사이즈는 품절입니다.{hint}")
            return False, (
                f"{product['name']} {color} {size} 사이즈는 재고가 {stock}개뿐입니다."
                f"{hint}")

        variant_id = colors[color]["variant_id"]

        # 이미 담긴 같은 상품·색·사이즈면 수량만 올린다 (줄을 늘리지 않는다)
        for item in self.cart:
            if item["variant_id"] == variant_id and item["size"] == size:
                if stock < item["quantity"] + quantity:
                    return False, (
                        f"이미 {item['quantity']}개가 담겨 있어 더 담을 수 없습니다. "
                        f"(재고 {stock}개)"
                    )
                item["quantity"] += quantity
                self._persist_cart_line(variant_id, size, item["quantity"])
                return True, (
                    f"{product['name']} {color} {size} 사이즈 수량을 "
                    f"{item['quantity']}개로 늘렸습니다."
                )

        self.cart.append({"product": product, "color": color,
                          "variant_id": variant_id, "size": size,
                          "quantity": quantity})
        self._persist_cart_line(variant_id, size, quantity)
        return True, (
            f"{product['name']} {color} {size} 사이즈 {quantity}개를 "
            f"장바구니에 담았습니다.")

    def remove_from_cart(self, product_id, size=None, color=None, quantity=None):
        """장바구니에서 상품을 빼거나 수량을 줄인다.

        size·color 생략 -> 그 상품을 색·사이즈 상관없이 전부 제거
        지정            -> 그 조합만
        quantity 지정   -> 그 수량만큼만 차감 (대상이 하나로 특정될 때만)

        담을 때와 달리 **뺄 때는 색을 요구하지 않는다.** 이미 담긴 것 중에서
        고르는 일이라 후보가 유한하고, 애매하면 아래에서 되묻기 때문이다.

        무엇을 얼마나 뺐는지 메시지에 구체적으로 남긴다.
        ("방금 삭제한 것 다시 담아줘" 를 처리하려면 이름·사이즈·수량이 필요하다)

        확인 절차는 여기 없다. agent/tools.py 가 담당한다.
        화면의 ✕ 버튼은 사용자가 직접 누른 것이므로 이미 확인이고,
        오해의 여지가 있는 것은 에이전트가 대화로 지우는 경우뿐이다.

        agent/tools.py 의 description 과 이 규칙이 일치해야 한다.
        어긋나면 파이썬은 아무 말도 하지 않고 에이전트만 조용히 틀린 답을 한다.

        반환: (bool, str)
        """
        size = self._size_key(size)
        if quantity is not None:
            checked = _positive_int(quantity, "수량")
            if isinstance(checked, str):
                return False, checked
            quantity = checked

        targets = [
            item for item in self.cart
            if item["product"]["id"] == product_id
            and (size is None or item["size"] == size)
            and (color is None or item["color"] == color)
        ]

        if not targets:
            return False, "장바구니에 해당 상품이 없습니다."

        if quantity is not None:
            # 어느 항목에서 뺄지 애매하면 임의로 고르지 말고 되묻는다.
            # 실패 메시지에 선택지를 실어 보내면 모델이 사용자에게 확인하거나
            # 스스로 사이즈를 지정해 다시 호출한다.
            if len(targets) > 1:
                where = ", ".join(
                    f"{t['color']} {t['size']}" for t in targets)
                return False, f"어느 것에서 뺄지 알려주세요. 담긴 것: {where}"

            item = targets[0]
            name = item["product"]["name"]

            # 담긴 수량보다 많이 빼달라고 하면 에러 대신 통째로 제거한다.
            # 수량이 0 인 줄이 남지 않게 하려는 목적도 있다.
            label = f"{name} {item['color']} {item['size']} 사이즈"
            if quantity >= item["quantity"]:
                removed_qty = item["quantity"]
                self.cart.remove(item)
                self._persist_cart_line(item["variant_id"], item["size"], 0)
                return True, f"{label} {removed_qty}개를 장바구니에서 뺐습니다."

            item["quantity"] -= quantity
            self._persist_cart_line(item["variant_id"], item["size"], item["quantity"])
            return True, f"{label} 수량을 {item['quantity']}개로 줄였습니다."

        # 무엇을 뺐는지 구체적으로 남긴다.
        # "2개 항목을 뺐습니다" 로만 답하면 모델이 다시 담을 수 없다.
        # ("방금 삭제한 것 다시 담아줘" 를 처리하려면 이름·사이즈·수량이 필요하다)
        detail = ", ".join(
            f"{item['product']['name']} {item['color']} "
            f"{item['size']} 사이즈 {item['quantity']}개"
            for item in targets
        )

        # targets 와 self.cart 는 다른 리스트라 순회 중 제거해도 안전하다.
        # (자기가 돌고 있는 리스트를 건드리면 인덱스가 밀려 항목을 건너뛴다)
        for item in targets:
            self.cart.remove(item)
            self._persist_cart_line(item["variant_id"], item["size"], 0)

        # "일부 사이즈만 빼시려면..." 안내는 tools 의 확인 단계에서 이미 했으므로
        # 실행 후에는 붙이지 않는다. (이미 지운 뒤에 알려주면 늦다)
        return True, f"{detail}를 장바구니에서 뺐습니다."

    def preview_removal(self, product_id, size=None, color=None, quantity=None):
        """무엇이 빠질지 미리 계산한다. 장바구니를 바꾸지 않는다.

        되돌릴 수 없는 작업은 실행 전에 사용자에게 보여줘야 합니다.
        can_cancel / can_return 이 "판정만 하고 상태는 안 바꾼다" 인 것과 같은 역할입니다.

        확인 절차 자체는 agent/tools.py 가 담당합니다.
        화면의 ✕ 버튼은 사용자가 직접 누른 것이므로 확인이 필요 없고,
        에이전트가 대화로 지우는 경우만 오해의 여지가 있기 때문입니다.

        반환: [{"product_id", "name", "size", "quantity"}] - 빠질 항목들. 없으면 빈 리스트.
        """
        size = self._size_key(size)
        targets = [
            item for item in self.cart
            if item["product"]["id"] == product_id
            and (size is None or item["size"] == size)
            and (color is None or item["color"] == color)
        ]

        if quantity is not None and len(targets) == 1:
            item = targets[0]
            return [{
                "product_id": item["product"]["id"],
                "name": item["product"]["name"],
                "color": item["color"],
                "size": item["size"],
                "quantity": min(quantity, item["quantity"]),
            }]

        return [{
            "product_id": item["product"]["id"],
            "name": item["product"]["name"],
            "color": item["color"],
            "size": item["size"],
            "quantity": item["quantity"],
        } for item in targets]

    def view_cart(self):
        """장바구니 내용과 합계를 반환한다.

        self.cart 를 그대로 돌려주지 않고 납작하게 펴서 준다.
        상품 dict 에는 description, material, care 가 들어 있어서 그대로 넘기면
        LLM 토큰이 낭비되고 모델이 엉뚱한 필드에 주목한다.

        원래 Tool 목록에 없었지만 반드시 필요한 메서드다.
        "장바구니에 뭐 들었어?"에 답할 방법이자, 결제 직전에 무엇을 사는지
        사용자에게 확인시킬 유일한 수단이다.

        반환:
            {"items": [...], "total": 합계 금액,
             "count": 항목(줄) 수, "quantity": 총 수량}

        count 와 quantity 는 다릅니다.
        같은 상품을 3개 담으면 줄은 하나(count=1)이고 수량은 셋(quantity=3)입니다.
        장바구니 배지에는 보통 quantity 를 씁니다.
        """
        items = []
        total = 0

        for entry in self.cart:
            product = entry["product"]
            subtotal = product["price"] * entry["quantity"]
            total += subtotal
            items.append({
                "product_id": product["id"],
                "variant_id": entry["variant_id"],
                "name": product["name"],
                "color": entry["color"],
                "size": entry["size"],
                "quantity": entry["quantity"],
                "price": product["price"],
                "subtotal": subtotal,
            })

        return {
            "items": items,
            "total": total,
            "count": len(items),
            "quantity": sum(item["quantity"] for item in items),
        }

    def _resolve_selection(self, selection):
        """주문할 대상을 장바구니 줄과 수량으로 확정한다.

        selection 이 비어 있으면 장바구니 전체입니다.
        형식: [{"product_id", "size"(선택), "quantity"(선택)}]

        대상 찾는 규칙은 preview_removal 과 같게 맞췄습니다.
        같은 말("어반 러너 270")이 빼기와 주문에서 다르게 해석되면
        사용자가 본 것과 실행된 것이 달라지기 때문입니다.

        반환: ([(장바구니 줄, 주문할 수량)], 오류 메시지 또는 None)
        """
        if not selection:
            return [(entry, entry["quantity"]) for entry in self.cart], None

        # 같은 대상을 두 번 적었으면 합칩니다. 나눠 적어서 수량 검사를 지나가면
        # 장바구니에 있는 것보다 많이 주문됩니다 (빼기에서 겪은 문제와 같습니다).
        merged = {}
        order = []
        for want in selection:
            if not isinstance(want, dict):
                return [], "주문할 항목의 형식이 올바르지 않습니다."
            key = (want.get("product_id"), self._size_key(want.get("size")), want.get("color"))
            if key not in merged:
                merged[key] = None
                order.append(key)
            quantity = want.get("quantity")
            if quantity is not None:
                merged[key] = (merged[key] or 0) + int(quantity)

        picked = []
        seen = set()
        for product_id, size, color in order:
            quantity = merged[(product_id, size, color)]
            rows = [entry for entry in self.cart
                    if entry["product"]["id"] == product_id
                    and (size is None or entry["size"] == size)
                    and (color is None or entry["color"] == color)]
            if not rows:
                product = self.get_product(product_id)
                name = product["name"] if product else product_id
                where = "".join([
                    f" {color}" if color is not None else "",
                    f" {size} 사이즈" if size is not None else "",
                ])
                return [], f"{name}{where} 는 장바구니에 없습니다."

            for entry in rows:
                if id(entry) in seen:
                    return [], "같은 상품을 여러 번 지정했습니다. 한 번만 적어 주세요."
                seen.add(id(entry))
                # 수량은 줄이 하나일 때만 의미가 있습니다.
                # 사이즈를 생략해 여러 줄이 잡혔으면 그 줄들을 통째로 주문합니다.
                take = entry["quantity"]
                if quantity is not None and len(rows) == 1:
                    take = min(quantity, entry["quantity"])
                if take <= 0:
                    return [], "주문 수량은 1개 이상이어야 합니다."
                picked.append((entry, take))

        return picked, None

    def preview_checkout(self, selection=None):
        """결제하면 무엇이 주문될지 미리 계산한다. 장바구니를 바꾸지 않는다.

        preview_removal / can_cancel 과 같은 역할이다.
        되돌릴 수 없는 작업은 "무엇이 처리될지" 를 먼저 보여줄 수 있어야 한다.

        반환: (줄 목록, 오류 메시지 또는 None)
        """
        if not self.cart:
            return [], "장바구니가 비어 있습니다."

        targets, problem = self._resolve_selection(selection)
        if problem:
            return [], problem
        if not targets:
            return [], "주문할 항목이 없습니다."

        rows = []
        for entry, quantity in targets:
            product = entry["product"]
            color = entry["color"]
            stock = self.get_stock(product["id"], entry["size"], color)
            if stock < quantity:
                return [], (
                    f"{product['name']} {color} {entry['size']} 사이즈의 "
                    f"재고가 부족합니다. (요청 {quantity}개 / 재고 {stock}개)"
                )
            rows.append({
                "product_id": product["id"],
                "variant_id": entry["variant_id"],
                "name": product["name"],
                "color": color,
                "size": entry["size"],
                "quantity": quantity,
                "price": product["price"] * quantity,
            })
        return rows, None

    def checkout(self, selection=None):
        """장바구니의 상품을 주문으로 전환한다.

        selection 을 주면 **그 항목만** 주문하고 나머지는 장바구니에 남깁니다.
        비워 두면 전체를 주문합니다.
        형식은 preview_checkout 과 같습니다.

        반환: (bool, str, 생성된 주문 리스트)
        """
        if not self.cart:
            return False, "장바구니가 비어 있습니다.", []

        targets, problem = self._resolve_selection(selection)
        if problem:
            return False, problem, []
        if not targets:
            return False, "주문할 항목이 없습니다.", []

        # 담아둔 사이에 품절됐을 수 있으므로 결제 직전에 재고를 다시 확인한다.
        # 하나라도 부족하면 아무것도 차감하지 않고 통째로 실패시킨다.
        # (일부만 처리되면 사용자도 재고도 이상한 상태로 남는다)
        for entry, quantity in targets:
            stock = self.get_stock(entry["product"]["id"], entry["size"],
                                   entry["color"])
            if stock < quantity:
                return False, (
                    f"{entry['product']['name']} {entry['color']} "
                    f"{entry['size']} 사이즈의 재고가 부족합니다. "
                    f"(요청 {quantity}개 / 재고 {stock}개)"
                ), []

        today = self.today.isoformat()

        # 여기서부터가 한 덩어리입니다. 재고 차감과 주문 생성이 하나라도
        # 실패하면 전부 없던 일이 되어야 합니다.
        #
        # 위의 사전 확인은 좋은 실패 메시지를 만들기 위한 것이고, 실제 관문은
        # take_stock 의 조건부 UPDATE 입니다. 사전 확인만 믿으면 그 사이에
        # 재고가 빠져나가도 모릅니다.
        touched, pending, in_cart = [], [], []
        db.lock_checkout(self.conn)
        try:
            # 주문할 항목은 메모리의 장바구니로 정했지만, 그게 지금도 DB 에 그만큼
            # 있는지는 여기서 잠그고 다시 본다. 재고의 take_stock 과 같은 관문이다.
            for entry, quantity in targets:
                have = db.lock_cart_quantity(self.conn, self.user_id,
                                             entry["variant_id"], entry["size"])
                if have < quantity:
                    raise _CartChanged()
                in_cart.append(have)

            for entry, quantity in targets:
                size = entry["size"]
                if not db.take_stock(self.conn, entry["variant_id"],
                                     size, quantity):
                    raise _StockConflict(
                        f"{entry['product']['name']} {entry['color']}",
                        size, quantity)
                touched.append(entry["product"]["id"])

            for entry, quantity in targets:
                order = self._build_order(
                    entry["product"], entry["size"], quantity, today,
                    entry["color"], entry["variant_id"]
                )
                # 주문마다 바로 INSERT 하는 이유: _next_order_id 가 DB 의 주문을 보고
                # 다음 번호를 계산합니다. 나중에 몰아서 넣으면 한 번에
                # 여러 건을 살 때 같은 번호가 두 번 나옵니다.
                self.orders.append(order)
                self._persist_new_order(order)
                events.enqueue(
                    self.conn, "order_created", "order", order["order_id"],
                    {"user_id": self.user_id, "order_id": order["order_id"],
                     "product_id": order["product_id"],
                     "variant_id": order["variant_id"], "size": str(order["size"]),
                     "quantity": order["quantity"], "price": order["price"],
                     "source": "checkout"},
                )
                pending.append(order)

            # 재고·주문·장바구니·outbox가 모두 한 트랜잭션이어야 한다.
            # 예전처럼 먼저 주문을 commit하고 뒤에서 장바구니를 지우면 중간 장애 때
            # 결제된 상품이 장바구니에 남는다.
            # 메모리 수량이 아니라 방금 잠근 DB 수량에서 뺀다.
            for (entry, quantity), have in zip(targets, in_cart):
                self._persist_cart_line(
                    entry["variant_id"], entry["size"],
                    have - quantity, commit=False)
        except Exception as failure:
            self.conn.rollback()
            for order in pending:
                self.orders.remove(order)
            if isinstance(failure, _CartChanged):
                self.cart = db.fetch_cart(self.conn, self.products, self.user_id)
                return False, (
                    "장바구니가 방금 다른 곳에서 바뀌어 결제하지 않았습니다. "
                    "장바구니를 다시 확인해 주세요."
                ), []
            if isinstance(failure, _StockConflict):
                return False, (
                    f"{failure.name} {failure.size} 사이즈의 재고가 부족합니다. "
                    f"(요청 {failure.quantity}개) "
                    f"방금 다른 곳에서 빠져나갔을 수 있습니다."
                ), []
            raise
        self.conn.commit()

        for pid in dict.fromkeys(touched):
            self._sync_stock(pid)

        # 주문한 만큼만 장바구니에서 뺍니다. 남은 것은 그대로 둡니다.
        # DB 는 위의 주문 트랜잭션 안에서 이미 반영했다. 메모리는 DB 를 따른다.
        self.cart = db.fetch_cart(self.conn, self.products, self.user_id)

        total = sum(order["price"] for order in pending)
        message = f"{len(pending)}건의 주문이 접수되었습니다. 결제 금액 {total:,}원"
        if self.cart:
            left = sum(entry["quantity"] for entry in self.cart)
            message += f" (장바구니에 {left}개가 남아 있습니다)"
        return True, message, pending

    def _build_order(self, product, size, quantity, today, color, variant_id):
        """checkout 이 장바구니 한 줄로 주문 dict 을 만든다.

        이름·색·가격을 주문에 박아 둔다. 정규화 위반이 아니라 주문 시점의 값을
        박제하기 위함이다 — 색이 단종되거나 이름이 바뀌어도 과거 주문은 그때
        산 것을 그대로 보여 줘야 한다.
        """
        return {
            "order_id": self._next_order_id(),
            "variant_id": variant_id,
            "product_id": product["id"],
            "product_name": product["name"],
            "color": color,
            "size": size,
            "quantity": quantity,
            "price": product["price"] * quantity,
            "ordered_at": today,
            "shipped_at": None,
            "delivered_at": None,
            "cancelled_at": None,
            "returned_at": None,
            "status": STATUS_PREPARING,
        }

    def _next_order_id(self):
        """ORD-1004 처럼 기존 번호에 이어서 발급한다.

        **모든 사용자의** 주문을 통틀어 DB 에서 다음 번호를 받는다. 이 Store 의
        self.orders 는 한 사용자 것만 들고 있어서, 그것만 보고 번호를 매기면 다른
        사용자의 주문과 같은 번호가 나와 PRIMARY KEY 에서 터진다. checkout 은
        db.lock_checkout() 안에서 이걸 부르므로 동시 결제도 번호가 겹치지 않는다.
        """
        return f"ORD-{db.next_order_number(self.conn)}"

    # ==================================================================
    # 주문 조회
    # ==================================================================

    def search_orders(self, keyword=None, status=None, ordered_within_days=None,
                      ordered_days_ago=None, delivered_within_days=None,
                      ordered_from=None, ordered_to=None):
        """조건에 맞는 주문 목록을 반환한다.

        날짜 조건이 셋인 이유가 있다.

          ordered_within_days=1   오늘과 어제를 모두 포함한다
          ordered_days_ago=1      정확히 어제 주문한 것만
          delivered_within_days=7 주문일이 아니라 "받은 날" 기준
          ordered_from/to         "2026-08-01 ~ 2026-08-31" 처럼 기간을 못박는다

        "어제 주문한 운동화 취소해줘" 에 ordered_within_days=1 을 쓰면
        오늘 주문한 것까지 걸려서 엉뚱한 주문을 취소할 수 있다.
        "지난주에 받은 셔츠" 는 주문일이 아니라 수령일로 찾아야 한다.
        주문일과 수령일은 며칠씩 차이가 나므로 같은 것으로 쓰면 빗나간다.

        반환: 주문 dict 리스트. 최신 주문이 앞에 온다.
        """
        results = []

        for order in self.orders:
            if keyword:
                # 상품명뿐 아니라 카테고리로도 찾을 수 있어야 한다.
                # 사용자는 "어반 러너 블랙"이 아니라 "운동화"라고 말한다.
                product = self.get_product(order["product_id"])
                category = product["category"] if product else ""
                haystack = f"{order['product_name']} {category}"
                if keyword.lower() not in haystack.lower():
                    continue

            if status is not None and order["status"] != status:
                continue

            if ordered_within_days is not None:
                # 날짜가 문자열로 저장되어 있으므로 계산하려면 date 로 바꿔야 한다.
                ordered = date.fromisoformat(order["ordered_at"])
                if (self.today - ordered).days > ordered_within_days:
                    continue

            if ordered_days_ago is not None:
                ordered = date.fromisoformat(order["ordered_at"])
                if (self.today - ordered).days != ordered_days_ago:
                    continue

            if delivered_within_days is not None:
                if order["delivered_at"] is None:
                    continue          # 아직 못 받은 주문은 "받은 날" 조건에 걸리지 않는다
                delivered = date.fromisoformat(order["delivered_at"])
                if (self.today - delivered).days > delivered_within_days:
                    continue

            # 기간. "지난달 주문" 처럼 달로 끊어 볼 때 쓴다.
            # 상대 일수(N일 전)로는 달의 경계를 정확히 표현할 수 없다.
            if ordered_from is not None and order["ordered_at"] < ordered_from:
                continue
            if ordered_to is not None and order["ordered_at"] > ordered_to:
                continue

            results.append(order)

        return sorted(results, key=lambda order: order["ordered_at"], reverse=True)

    def get_order(self, order_id):
        """주문 ID 로 주문 하나를 찾는다. 없으면 None."""
        for order in self.orders:
            if order["order_id"] == order_id:
                return order
        return None

    # ==================================================================
    # 정책 판정  ← 이 프로젝트 설계의 핵심
    #
    # 아래 can_* 메서드는 상태를 바꾸지 않습니다. 판단만 합니다.
    # 실행 메서드(cancel_order 등)는 반드시 내부에서 can_* 를 다시 호출합니다.
    # 그래야 에이전트가 확인 단계를 건너뛰어도 안전합니다.
    # ==================================================================

    def can_cancel(self, order_id) -> Decision:
        """이 주문을 지금 취소할 수 있는가? (상태를 바꾸지 않는다)

        판정 기준은 status 문자열이 아니라 shipped_at / delivered_at 이다.
        상태 문자열은 표시용이고, 날짜 필드가 사실이기 때문이다.

        취소가 불가능할 때 alternative 를 채우는 것이 이 메서드의 핵심이다.
        "이미 배송이 시작됐다면 다른 방법을 안내해줘" 에 답하려면, 왜 안 되는지와
        그럼 어떻게 하는지를 함께 돌려줘야 한다.
        """
        order = self.get_order(order_id)
        if order is None:
            return Decision(False, f"'{order_id}' 주문을 찾을 수 없습니다.")

        if order["status"] == STATUS_CANCELLED:
            return Decision(False, "이미 취소된 주문입니다.")

        if order["status"] == STATUS_RETURN_REQUESTED:
            return Decision(False, "이미 반품이 접수된 주문입니다.")

        # 발송 전이면 언제든 취소 가능
        if order["shipped_at"] is None:
            return Decision(True, "아직 발송 전이라 바로 취소할 수 있습니다.")

        # 발송됐지만 아직 수령 전 -> 취소 불가, 수령 후 반품 안내
        if order["delivered_at"] is None:
            return Decision(
                False,
                f"{order['shipped_at']}에 발송되어 배송 중이므로 취소할 수 없습니다.",
                "상품을 수령하신 뒤 반품을 신청하시면 환불받으실 수 있습니다.",
            )

        # 이미 수령 -> 반품이 가능한지 확인해서 안내에 반영
        return_decision = self.can_return(order_id)
        alternative = (
            f"대신 반품 신청이 가능합니다. {return_decision.reason}"
            if return_decision.allowed
            else return_decision.reason
        )
        return Decision(
            False,
            f"{order['delivered_at']}에 배송이 완료되어 취소할 수 없습니다.",
            alternative,
        )

    def return_deadline(self, order_id):
        """반품 마감일(문자열). 아직 수령 전이거나 없는 주문이면 None.

        can_return 이 문장 안에 넣어 주는 값을 따로 꺼내 쓸 수 있게 한 것이다.
        문장에서 날짜를 다시 파싱하게 하면 그게 곧 두 번째 판단자가 된다.
        """
        order = self.get_order(order_id)
        if order is None or order["delivered_at"] is None:
            return None
        delivered = date.fromisoformat(order["delivered_at"])
        return (delivered + timedelta(days=RETURN_PERIOD_DAYS)).isoformat()

    def can_return(self, order_id) -> Decision:
        """이 주문을 반품할 수 있는가? (상태를 바꾸지 않는다)

        반품 마감일은 데이터에 저장하지 않고 여기서 계산한다.
        정책은 데이터가 아니라 코드에 있어야 나중에 바꾸기 쉽다.
        기간을 30일로 늘리려면 RETURN_PERIOD_DAYS 한 줄만 고치면 된다.
        """
        order = self.get_order(order_id)
        if order is None:
            return Decision(False, f"'{order_id}' 주문을 찾을 수 없습니다.")

        if order["status"] == STATUS_CANCELLED:
            return Decision(False, "취소된 주문은 반품할 수 없습니다.")

        if order["status"] == STATUS_RETURN_REQUESTED:
            return Decision(False, "이미 반품이 접수된 주문입니다.")

        # 아직 못 받았으면 반품이라는 개념 자체가 성립하지 않는다
        if order["delivered_at"] is None:
            if order["shipped_at"] is None:
                return Decision(
                    False,
                    "아직 발송되지 않아 반품할 수 없습니다.",
                    "발송 전이므로 주문 취소가 가능합니다.",
                )
            return Decision(
                False,
                "아직 배송 중이라 반품할 수 없습니다.",
                "상품을 수령하신 뒤에 반품을 신청해 주세요.",
            )

        # 날짜가 문자열이므로 date 로 바꿔서 계산한다
        delivered = date.fromisoformat(order["delivered_at"])
        deadline = delivered + timedelta(days=RETURN_PERIOD_DAYS)
        days_left = (deadline - self.today).days

        if days_left < 0:
            return Decision(
                False,
                f"반품 가능 기간이 {-days_left}일 지났습니다. "
                f"(수령 {order['delivered_at']}, 기한 {deadline.isoformat()})",
            )

        return Decision(
            True,
            f"수령 후 {RETURN_PERIOD_DAYS}일 이내로 반품 가능하며 "
            f"{days_left}일 남았습니다. (기한 {deadline.isoformat()})",
        )

    # ==================================================================
    # 정책 실행 (상태를 바꿈)
    # ==================================================================

    def cancel_order(self, order_id):
        """주문을 취소한다.

        첫 줄에서 can_cancel 을 다시 호출하는 것이 중요하다.
        에이전트가 cancel_possible 을 건너뛰고 바로 이 Tool 을 부를 수 있으므로,
        판정과 실행이 각자 판단하면 어긋난다. 판단은 언제나 can_cancel 한 곳이다.

        반환: (bool, str)
        """
        decision = self.can_cancel(order_id)
        if not decision.allowed:
            message = decision.reason
            if decision.alternative:
                message += f" {decision.alternative}"
            return False, message

        order = self.get_order(order_id)
        expected_status = order["status"]
        order["status"] = STATUS_CANCELLED
        order["cancelled_at"] = self.today.isoformat()

        # 재고 원복. checkout 으로 만들어진 주문이든 데모 주문이든 동일하게 처리한다.
        # 상태 변경과 재고 원복은 한 덩어리다 — 취소는 됐는데 재고가 안 돌아오면
        # 그 재고는 영구히 사라진다.
        db.lock_checkout(self.conn)
        try:
            if not self._persist_order_state(order, expected_status):
                self.conn.rollback()
                self._reload_order(order_id)
                return False, "주문 상태가 방금 바뀌어 취소하지 않았습니다. 주문을 다시 확인해 주세요."
            # 산 **그 색**으로 돌려준다. 상품 단위로 돌리면 어느 색이
            # 늘었는지 알 수 없고, 다른 색 재고가 부풀어 오른다.
            db.give_back_stock(
                self.conn, order["variant_id"], order["size"], order["quantity"]
            )
            events.enqueue(
                self.conn, "order_cancelled", "order", order_id,
                {"user_id": self.user_id, "order_id": order_id,
                 "product_id": order["product_id"],
                 "variant_id": order["variant_id"], "size": str(order["size"]),
                 "quantity": order["quantity"], "source": "runtime"},
            )
        except Exception:
            self.conn.rollback()
            raise
        self.conn.commit()
        self._sync_stock(order["product_id"])

        return True, (
            f"{order_id} {order['product_name']} 주문이 취소되었습니다. "
            f"{order['price']:,}원이 환불됩니다."
        )

    def return_order(self, order_id, reason=None):
        """반품을 신청한다. cancel_order 와 같은 패턴이다.

        반환: (bool, str)
        """
        decision = self.can_return(order_id)
        if not decision.allowed:
            message = decision.reason
            if decision.alternative:
                message += f" {decision.alternative}"
            return False, message

        order = self.get_order(order_id)
        expected_status = order["status"]
        order["status"] = STATUS_RETURN_REQUESTED
        order["returned_at"] = self.today.isoformat()
        order["return_reason"] = reason
        if not self._persist_order_state(order, expected_status):
            self.conn.rollback()
            self._reload_order(order_id)
            return False, "주문 상태가 방금 바뀌어 반품을 접수하지 않았습니다. 주문을 다시 확인해 주세요."
        events.enqueue(
            self.conn, "return_requested", "order", order_id,
            {"user_id": self.user_id, "order_id": order_id,
             "product_id": order["product_id"], "reason": reason,
             "source": "runtime"},
        )
        self.conn.commit()

        return True, (
            f"{order_id} {order['product_name']} 반품이 접수되었습니다. "
            f"회수 완료 후 {order['price']:,}원이 환불됩니다."
        )
