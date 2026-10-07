"""web/ 프런트엔드를 실제 Store·Agent 에 붙이는 HTTP 서버.

    python3 server.py            # http://127.0.0.1:8000
    python3 server.py --port 9000

web/api.js 가 요구하는 엔드포인트를 그대로 구현합니다.

starlette + uvicorn 만 씁니다. FastAPI 를 얹으면 pydantic 모델 정의가 늘어나는데
이 규모의 API 에서는 얻는 게 없습니다.

HTTP 는 요청마다 상태를 어디선가 읽어야 하고, 그게 DB 입니다. 장바구니를 담는
요청과 그것을 조회하는 요청이 서로 다른 요청이기 때문입니다.


사용자와 세션
-------------
브라우저가 처음 오면 user_id 와 세션을 하나 만들어, 세션의 랜덤 토큰을 쿠키(sid)로
심습니다. 이후 모든 요청은 그 토큰으로 사용자를 찾습니다. 쿠키에 user_id 를 직접 담지
않는 이유는, user_id 가 이벤트·로그에 남기 때문입니다 — 그걸 본 사람이 쿠키를 만들어
그 사용자가 될 수 있었습니다. 로그인은 없습니다 — "이 브라우저 = 이 사용자" 입니다.
사용자마다 Store(장바구니·주문은 그 사용자 것만) 와 Agent 를 하나씩 두고,
`sessions` 에 모아 둡니다. 임베딩 행렬과 질의 모델은 Store 들이 공유합니다.

새 사용자에게는 데모 주문 14건을 그 사용자 것으로 심어 줍니다. 그래야 새 브라우저에서도
"어제 주문한 거 취소해줘" 를 바로 해 볼 수 있습니다.


확인 대기(승인 버튼)는 DB 에 있다
--------------------------------
/api/chat 이 미리보기를 만들고 /api/approve 가 실행하는데, 둘은 서로 다른 요청입니다.
그 사이의 상태(PendingAction·미룬 작업·대화 내역)를 Agent 객체 안에만 두면 서버를
재시작한 순간 화면의 승인 버튼이 아무것도 가리키지 않게 됩니다. 그래서 agent 를 쓰는
요청이 끝날 때마다 `agent.snapshot()` 을 agent_state 테이블에 저장하고, 세션을 새로
만들 때 되살립니다.

만료는 턴이 아니라 **시간**입니다 (config.PENDING_TTL_SECONDS, 기본 5분). HTTP 에는
턴이 없어서 — 사용자가 아무 말 없이 10분 뒤 버튼을 누를 수 있어서 — 버튼이 떠 있는
동안 장바구니·주문 상태가 바뀌었을 가능성을 시간으로 자릅니다. 만료된 승인은 실행하지
않고 다시 요청하라고 안내합니다. 실행 직전에 미리보기를 다시 계산해 비교하는 장치는
그대로 남아 있어, 만료 전이라도 상태가 달라졌으면 실행되지 않습니다.


남은 한계
---------
- 세션 쿠키는 인증이 아닙니다. 쿠키를 아는 사람은 그 장바구니를 봅니다.
- 사용자마다 Store 가 상품 dict 를 따로 들고 있어서, 다른 사용자가 결제한 재고는
  이 사용자의 화면에 바로 반영되지 않습니다. 실제 차감은 DB 의 조건부 UPDATE 가
  판정하므로 틀린 결제는 일어나지 않습니다.
- 프로세스가 여럿이면 `sessions` 가 갈라집니다. 상태는 DB 에 있으니 되살릴 수는
  있지만, 같은 사용자의 요청이 동시에 두 프로세스에 가면 순서를 보장하지 못합니다.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import OrderedDict
from datetime import date

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from shopmate.agent import verifier
from shopmate import config
from shopmate.store import db
from shopmate.store import events
from shopmate.search import photo_store
from shopmate.store import session_state
from shopmate.search import photo_analysis
from shopmate.agent.loop import ShoppingAgent, tool_history_messages
from shopmate.store.shop import Store
from shopmate.agent.tools import MAX_SEARCH_RESULTS

WEB_DIR = config.PROJECT_ROOT / "web"

# 업무 DB 의 접속 문자열 (웹 서버 전용 최소 권한 계정).
DSN = config.APP_SHOP_DSN

SESSION_COOKIE = "sid"
MAX_SESSIONS = 64                         # 메모리에 올려 둘 세션 수. 넘으면 오래된 것부터 내린다


# --------------------------------------------------------------------------
# 세션 — 사용자 하나의 실행 문맥
# --------------------------------------------------------------------------

class Session:
    """사용자 한 명의 Store·Agent·대화 내역과, 그것들을 한 번에 한 요청만 쓰게 하는 락.

    대화 한 턴은 모델 호출을 여러 번 하느라 수십 초가 걸립니다. 그걸 async 핸들러
    안에서 동기로 부르면 이벤트 루프가 통째로 멈춰서, 모델이 생각하는 동안
    장바구니 버튼도 상품 목록도 응답하지 않습니다 (실측: /api/cart 타임아웃).
    그래서 agent 를 부르는 일은 스레드풀로 보내고, 같은 사용자의 agent 요청끼리만
    이 락으로 줄을 세웁니다. 장바구니·상품 조회는 락을 잡지 않습니다 — 밀리초짜리
    요청이고, 채팅을 기다리는 동안 화면을 계속 쓸 수 있어야 하기 때문입니다.
    채팅 중 화면에서 장바구니를 바꾸면 승인 시점에 미리보기를 다시 계산해 비교하는
    장치(agent.approve)가 그것을 잡습니다.
    """

    def __init__(self, user_id, dsn):
        self.user_id = user_id
        self.dsn = dsn
        self.store = Store(db_path=dsn, user_id=user_id)
        self.agent = ShoppingAgent(self.store)
        self.history: list[dict] = []
        self.lock = threading.Lock()

        # 서버를 재시작했거나 세션이 메모리에서 내려갔다가 다시 올라온 경우.
        state = _session_state_read(user_id)
        if state:
            self.agent.restore(state)
            self.history = list(state.get("history") or [])
        self.load_preferences()

    def load_preferences(self):
        """명시적 선호를 업무 DB 에서 읽는다. 원본은 user_preferences 하나뿐이다."""
        entries = _preferences_read(self.dsn, self.user_id)
        if entries is None:
            self._saved_preferences = None
            return
        self.agent.preferences = verifier.PreferenceMemory(entries)
        self._saved_preferences = self.agent.preferences.to_dict()

    def save(self):
        """확인 대기·미룬 작업·대화 내역을 세션 DB 에 둔다. agent 를 쓴 요청 끝에 부른다.

        업무 DB 가 아니라 **세션 DB** 입니다. 대화 상태는 수명주기가 짧아
        주문·재고와 같은 DB 에 두지 않습니다.

        선호만은 예외로 **업무 DB** 에 둡니다. 사용자가 "기억해 줘" 라고 한 것이라
        세션 DB(UNLOGGED, 대화 상태와 함께 청소됨)에 두면 사라질 수 있습니다.
        바뀌었을 때만 씁니다.
        """
        preferences = self.agent.preferences.to_dict()
        if preferences != self._saved_preferences and _preferences_write(
                self.dsn, self.user_id, preferences):
            self._saved_preferences = preferences
        snapshot = {**self.agent.snapshot(), "history": self.history}
        snapshot.pop("preferences", None)
        _session_state_write(self.user_id, snapshot)

    def close(self):
        if self.store.conn is not None:
            self.store.conn.close()


# 세션 상태는 요청 때마다 짧게 붙었다 끊는다.
#
# 세션을 최대 MAX_SESSIONS 개 들고 있고 각자 업무 DB 연결을 하나씩 잡습니다.
# 여기서 세션 DB 연결까지 세션마다 들고 있으면 연결 수가 두 배가 되고,
# PostgreSQL 기본 max_connections(100) 에 금방 닿습니다. 상태 저장은 한 턴에
# 한 번, 수 ms 짜리 쓰기라 매번 새로 붙어도 손해가 없습니다.
# (연결을 오래 쥐어야 할 만큼 트래픽이 커지면 그때 풀을 답니다 — pgbouncer /
#  psycopg_pool. 지금 넣으면 재는 것 없이 늘어나는 부품입니다.)

# 세션 DB 가 죽어도 상점은 뜬다.
#
# 여기 있는 것은 "확인 대기·대화 내역" 이고, 상품·재고·주문은 업무 DB 에
# 있습니다. 세션 DB 하나 때문에 상점 전체가 500 을 내는 것은 과한 결합입니다.
# 대신 조용히 넘어가지는 않습니다 — 실패는 매번 로그에 남깁니다. 잃은 것을
# 숨기는 것이 데이터를 잃는 것보다 나쁘기 때문입니다.
def _session_state(action, user_id, *args):
    try:
        with session_state.connect() as conn:
            return getattr(session_state, action)(conn, user_id, *args)
    except Exception as problem:
        print(f"[세션 DB] {action}({user_id}) 실패: {problem}")
        return None


def _session_state_read(user_id):
    return _session_state("load_agent_state", user_id)


def _session_state_write(user_id, state):
    _session_state("save_agent_state", user_id, state)


def _session_state_delete(user_id):
    _session_state("delete_agent_state", user_id)


# 선호는 업무 DB 에 있다 (Session.save 주석). 세션 DB 와 같은 이유로 실패해도 상점은
# 뜨게 두되, 매번 로그에 남긴다.
def _preferences_read(dsn, user_id):
    try:
        with db.connect(dsn) as conn:
            return db.fetch_preferences(conn, user_id)
    except Exception as problem:
        print(f"[선호] 읽기({user_id}) 실패: {problem}")
        return None


def _preferences_write(dsn, user_id, entries):
    try:
        with db.connect(dsn) as conn:
            db.save_preferences(conn, user_id, entries)
        return True
    except Exception as problem:
        print(f"[선호] 저장({user_id}) 실패: {problem}")
        return False


sessions: "OrderedDict[str, Session]" = OrderedDict()
_sessions_lock = threading.Lock()


def get_session(user_id):
    """사용자의 세션. 없으면 만든다 (새 사용자면 데모 주문도 심는다)."""
    with _sessions_lock:
        found = sessions.get(user_id)
        if found is not None:
            sessions.move_to_end(user_id)
            return found

        conn = db.connect(DSN)
        try:
            if db.ensure_user(conn, user_id):
                # 처음 온 사용자. 사용자 종류에 맞는 처음 주문을 심는다 — 데모 사용자(info1~5)는
                # 각자 다른 시나리오, 익명 사용자는 주문 없음 (db.seed_user_orders).
                db.seed_user_orders(conn, user_id)
        finally:
            conn.close()

        session = Session(user_id, DSN)
        # Store 가 뜨면서 장바구니·주문을 읽어 트랜잭션이 열린다. 요청 끝 정리는 요청이 있어야
        # 도는데, 기동할 때 만드는 기본 사용자(demo) 세션은 요청이 없어 영영 열린 채 남았다.
        db.end_read_transaction(session.store.conn)
        sessions[user_id] = session
        while len(sessions) > MAX_SESSIONS:
            _, oldest = sessions.popitem(last=False)
            oldest.save()
            oldest.close()
        return session


def session_of(request):
    return get_session(request.state.user_id)


def _with_session(session, func, *args):
    """agent 를 쓰는 동기 함수를 그 사용자의 락 안에서 실행하고 상태를 저장한다."""
    with session.lock:
        try:
            result = func(*args)
            session.save()
            return result
        finally:
            with session.store.db_lock:
                db.end_read_transaction(session.store.conn)


async def _store_call(store, func, *args):
    """화면 요청의 Store 작업을 그 Store 의 db_lock 안에서, 스레드풀에서 실행한다.

    Store 연결 하나를 화면 요청과 에이전트 턴이 함께 쓴다. 예전에는 화면 쪽이 아무 잠금 없이
    불렀기 때문에, 상담이 결제·취소(여러 문장을 한 트랜잭션으로)를 하는 도중 장바구니 버튼의
    commit 이 끼어들어 그 트랜잭션을 중간에 확정할 수 있었다. 에이전트는 Tool 하나를 실행하는
    동안만 db_lock 을 쥐므로(Toolbox.call) 여기서 기다리는 시간은 길어야 Tool 하나다 — 모델
    응답(수십 초)을 기다리지 않는다. 잠금을 기다리는 동안 이벤트 루프가 멈추지 않게 스레드풀에서
    돈다. 끝나면 읽기로 열린 트랜잭션을 닫고 놓는다.
    """
    def run():
        with store.db_lock:
            try:
                return func(*args)
            finally:
                db.end_read_transaction(store.conn)
    return await run_in_threadpool(run)


# --------------------------------------------------------------------------
# 요청이 끝나면 Store 연결의 열린 트랜잭션을 닫는다
# --------------------------------------------------------------------------
# psycopg 는 autocommit 이 꺼져 있어 SELECT 하나에도 트랜잭션이 열린다. 쓰기는 모두 그 자리에서
# commit 하지만 읽기(상품 검색·장바구니 보기 …)는 아무도 닫지 않아서, 세션마다 Store 연결이
# 다음 쓰기까지 몇십 분씩 "idle in transaction" 으로 잠금을 쥐고 있었다. 그러면
#   - ALTER TABLE 같은 스키마 변경이 막힌다 (2026-09-29 v13 적용이 여기서 멈췄고, 기다리는
#     ALTER 뒤로 모든 상품 조회가 줄을 서서 떠 있는 서버까지 멎을 뻔했다)
#   - VACUUM 이 그 시점 이후의 죽은 행을 치우지 못한다
#   - 한 문장이 실패하면(statement_timeout 등) 연결이 오류 상태로 남아, rollback 전까지 그
#     사용자의 모든 요청이 실패한다
# 그래서 요청 끝(SessionMiddleware)과 에이전트 턴 끝(_with_session)에서 닫는다.

def _end_request_transaction(user_id):
    """HTTP 요청이 끝났을 때의 안전망. Store 작업은 _store_call·Toolbox.call 이 db_lock 안에서 하고
    끝에 스스로 닫지만, 그 밖의 경로가 남긴 트랜잭션도 여기서 닫는다. 누가 db_lock 을 쥐고
    있으면(여러 문장을 한 트랜잭션으로 묶는 중일 수 있다) 건드리지 않는다 — 기다리지 않는다."""
    with _sessions_lock:
        session = sessions.get(user_id)
    if session is None or not session.store.db_lock.acquire(blocking=False):
        return
    try:
        db.end_read_transaction(session.store.conn)
    finally:
        session.store.db_lock.release()


# 토큰 → user_id 를 잠깐 기억한다. 요청마다 DB 에 새로 붙지 않기 위해서다.
# 세션 폐기가 생기면 최대 이만큼 늦게 반영된다.
_SESSION_CACHE_SECONDS = 30
_SESSION_CACHE_SIZE = 1024
_session_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_session_cache_lock = threading.Lock()


def _resolve_session(token):
    now = time.monotonic()
    with _session_cache_lock:
        hit = _session_cache.get(token)
        if hit is not None and now - hit[1] < _SESSION_CACHE_SECONDS:
            return hit[0]
    with db.connect(DSN) as conn:
        user_id = db.resolve_web_session(conn, token)
    if user_id is not None:
        with _session_cache_lock:
            _session_cache[token] = (user_id, now)
            _session_cache.move_to_end(token)
            while len(_session_cache) > _SESSION_CACHE_SIZE:
                _session_cache.popitem(last=False)
    return user_id


def _issue_session():
    """새 익명 사용자와 세션을 만든다. 익명 사용자는 주문·장바구니 없이 시작한다 (db.seed_user_orders)."""
    user_id = db.new_user_id()
    with db.connect(DSN) as conn:
        db.ensure_user(conn, user_id)
        db.seed_user_orders(conn, user_id)
        token = db.create_web_session(conn, user_id, config.SESSION_MAX_AGE_SECONDS)
    return user_id, token


# --------------------------------------------------------------------------
# 만료 청소
# --------------------------------------------------------------------------
#
# 예전에는 기동할 때 한 번만 치웠다. 서버가 며칠 떠 있으면 만료된 승인·사진 질의·
# 세션이 그대로 쌓였다 (사진 원본은 MinIO 에도 남는다). 그래서 주기적으로 치운다.
# 한 가지가 실패해도 나머지는 치우고, 실패는 매번 로그에 남긴다.

def purge_expired_once():
    """만료된 승인 대기·오래된 대화 상태·사진 질의·브라우저 세션을 치운다.

    세션 DB 를 치웠으면 True. 업무 DB 쪽 실패는 로그만 남긴다.
    """
    session_ok = True
    try:
        with session_state.connect() as conn:
            cleared, idle = session_state.purge_expired(conn)
        if cleared or idle:
            print(f"[청소] 만료된 확인 대기 {cleared}건 · 오래된 대화 상태 {idle}건")
        images = photo_store.purge_expired()
        if images:
            print(f"[청소] 만료된 이미지 질의 {images}건")
    except Exception as problem:
        session_ok = False
        print(f"[청소] 세션 DB 실패: {problem}")
    try:
        with db.connect(DSN) as conn:
            expired = db.purge_expired_web_sessions(conn)
        if expired:
            print(f"[청소] 만료된 브라우저 세션 {expired}건")
    except Exception as problem:
        print(f"[청소] 업무 DB 실패: {problem}")
    return session_ok


def start_janitor(interval=None):
    """purge_expired_once 를 interval 초마다 도는 데몬 스레드. 서버 기동 때만 부른다."""
    interval = interval or config.PURGE_INTERVAL_SECONDS
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            purge_expired_once()

    threading.Thread(target=loop, name="janitor", daemon=True).start()
    return stop


def _needs_session(path):
    # 정적 파일(app.js·그림)마다 사용자를 만들지 않는다. 화면과 API 만.
    return path.startswith("/api/") or path == "/" or path.endswith(".html")


class SessionMiddleware(BaseHTTPMiddleware):
    """쿠키의 세션 토큰으로 user_id 를 찾고, 없으면 새 사용자·세션을 만들어 심는다.

    클라이언트가 보낸 값을 user_id 로 받아들이지 않는다. 모르는 토큰(예전 쿠키 포함)은
    없는 것과 같게 보고 새로 발급한다.
    """

    async def dispatch(self, request, call_next):
        token = request.cookies.get(SESSION_COOKIE)
        user_id = await run_in_threadpool(_resolve_session, token) if token else None
        issued = None
        if user_id is None and _needs_session(request.url.path):
            user_id, issued = await run_in_threadpool(_issue_session)
        request.state.user_id = user_id
        try:
            response = await call_next(request)
        finally:
            if user_id is not None:
                _end_request_transaction(user_id)
        # 엔드포인트가 이미 sid 를 심었으면(사용자 전환) 덮지 않는다.
        already = any(value.startswith(f"{SESSION_COOKIE}=")
                      for value in response.headers.getlist("set-cookie"))
        if issued and not already:
            response.set_cookie(SESSION_COOKIE, issued,
                                max_age=config.SESSION_MAX_AGE_SECONDS,
                                httponly=True, samesite="lax",
                                secure=config.SESSION_COOKIE_SECURE)
        return response


# --------------------------------------------------------------------------
# 직렬화 — 화면이 기대하는 모양으로 맞춘다
#
# web/mock.js 가 정의한 모양이 계약입니다. 필드 이름이 하나라도 다르면
# 화면이 조용히 빈칸을 그립니다.
# --------------------------------------------------------------------------

def product_json(store, product):
    """상품 하나. mock.js 의 buildProducts() 가 만드는 모양과 같게."""
    return {
        "id": product["id"],
        "name": product["name"],
        "brand": product["brand"],
        "group": store.group_of(product["category"]),
        "category": product["category"],
        "gender": product["gender"],
        # 색이 상품의 열이 아니라 variant 가 되었으므로 목록이 나갑니다.
        # color 는 카드에 한 점만 찍기 위한 **대표 색**(첫 번째)입니다.
        # 담을 때 쓰는 것은 colors 쪽입니다 — 대표 색으로 담으면 사용자가
        # 고르지 않은 색이 장바구니에 들어갑니다.
        "colors": [
            {
                "color": name,
                "sizes": {str(size): qty for size, qty in info["sizes"].items()},
                "in_stock": any(qty > 0 for qty in info["sizes"].values()),
            }
            for name, info in product.get("colors", {}).items()
        ],
        "color": next(iter(product.get("colors", {})), None),
        "material": product["material"],
        "material_detail": product.get("material_detail"),
        "price": product["price"],
        "rating": product["rating"],
        "review_count": product["review_count"],
        # 키를 문자열로 보냅니다. JSON 객체의 키는 문자열뿐이고,
        # 화면도 String(size) 로 비교하고 있습니다.
        "sizes": {str(size): qty for size, qty in product["sizes"].items()},
        "size_options": product.get("size_options") or [
            {"code": str(size), "label": str(size), "system": "legacy_numeric"}
            for size in product["sizes"]
        ],
        "machine_washable": product["machine_washable"],
        "care": product.get("care"),
        "delivery_days": product.get("delivery_days"),
        "description": product["description"],
        # 사진. 없으면 None 이고 화면이 그림으로 대신 그린다.
        # 브라우저는 MinIO 를 직접 보지 않는다 — 아래 /media 라우트가 중계한다.
        "image_url": media_url(product, "key"),
        "thumbnail_url": media_url(product, "thumbnail"),
    }


def attach_wishes(store, items):
    """상품 JSON 목록에 찜 수(wish_count)와 지금 사용자의 찜 여부(wished)를 붙인다 (v13).

    찜 수 = products.wish_base(리뷰 수에서 추정한 합성 시작값) + wishlists 실제 행 수.
    찜을 못 읽어도 상품은 보여야 하므로 실패는 로그만 남기고 시작값으로 채운다.
    """
    if not items:
        return items
    try:
        counts = db.wish_counts(store.conn, [p["id"] for p in items])
        mine = set(db.user_wishlist(store.conn, store.user_id))
    except Exception as problem:
        store.conn.rollback()
        print(f"[찜] 읽기 실패: {problem}")
        counts, mine = {}, set()
    for p in items:
        base = (store.get_product(p["id"]) or {}).get("wish_base") or 0
        p["wish_count"] = base + counts.get(p["id"], 0)
        p["wished"] = p["id"] in mine
    return items


def media_url(product, which):
    """상품 사진의 /media URL. 사진이 없으면 None."""
    media = (product or {}).get("media")
    if not media or not media.get(which):
        return None
    return f"/media/{media['bucket']}/{media[which]}"


def size_label(product, size):
    """사이즈 코드의 표시 이름. ONE_SIZE → "단일 사이즈", 270 → "270mm".

    상품의 size_options 에서 찾는다. 없으면(옛 데이터·상품 삭제) 코드를 그대로.
    장바구니·주문 줄은 상품 전체를 싣지 않으므로 여기서 이름을 붙여 보낸다.
    """
    for option in (product or {}).get("size_options") or []:
        if str(option.get("code")) == str(size):
            return option.get("label") or str(size)
    return str(size)


def screen_message(store, product_id, size, message):
    """화면(장바구니 버튼)에 돌려줄 Store 문장의 사이즈 코드를 표시 이름으로 바꾼다.

    Store 문장은 Tool 결과로 모델에게도 가므로 코드("ONE_SIZE")를 그대로 둔다 —
    모델은 그 코드를 다시 인자로 넘겨야 한다. 사람이 읽는 HTTP 응답에서만 바꾼다.
    """
    label = size_label(store.get_product(product_id), size)
    if not message or label == str(size):
        return message
    phrase = label if label.endswith("사이즈") else f"{label} 사이즈"
    return message.replace(f"{size} 사이즈", phrase)


def cart_json(store):
    """장바구니. {lines, quantity, total}"""
    view = store.view_cart()
    lines = []
    for item in view["items"]:
        product = store.get_product(item["product_id"])
        lines.append({
            "product_id": item["product_id"],
            # 장바구니 한 줄의 열쇠는 (상품, 색, 사이즈) 입니다. 색이 빠지면
            # 검은색 270 과 네이비 270 이 화면에서 같은 줄로 보입니다.
            "color": item.get("color") or "",
            "size": item["size"],
            "size_label": size_label(product, item["size"]),
            "quantity": item["quantity"],
            "name": item["name"],
            "brand": product["brand"] if product else "",
            "price": item["price"],
            "category": product["category"] if product else "",
            "group": store.group_of(product["category"]) if product else None,
            "thumbnail_url": media_url(product, "thumbnail"),
        })
    return {"lines": lines, "quantity": view["quantity"], "total": view["total"]}


def order_json(store, order):
    """주문 하나.

    모양을 하나 바꿔서 보냅니다. Store 의 주문은 상품 한 종류인데 화면은
    items 배열을 기대합니다. 한 건을 원소 하나인 배열로 감쌉니다.
    화면이 여러 상품 주문을 그릴 수 있게 만들어져 있으므로, 나중에 Store 가
    그렇게 바뀌어도 화면은 안 바뀝니다.
    """
    product = store.get_product(order["product_id"])
    ordered = date.fromisoformat(order["ordered_at"])
    return {
        "id": order["order_id"],
        "status": order["status"],
        "ordered_days_ago": (store.today - ordered).days,
        "items": [{
            "product_id": order["product_id"],
            "name": order["product_name"],
            "brand": product["brand"] if product else "",
            "size": order["size"],
            "size_label": size_label(product, order["size"]),
            "quantity": order["quantity"],
            "price": product["price"] if product else order["price"],
            "category": product["category"] if product else "",
            # 주문한 그때의 색입니다. 상품에서 다시 읽으면 안 됩니다 —
            # 상품에는 이제 색이 여럿이고, 그중 무엇을 샀는지는 주문만 압니다.
            "color": order.get("color") or "",
            "group": store.group_of(product["category"]) if product else None,
            "thumbnail_url": media_url(product, "thumbnail"),
        }],
        "total": order["price"],
        # 화면의 취소·반품 버튼이 쓰는 판정. 에이전트가 Tool 로 묻는 것과 **같은 함수**
        # (store.can_cancel / can_return)라서, 화면과 에이전트가 같은 규칙을 본다.
        "actions": {
            "cancel": _decision_json(store.can_cancel(order["order_id"])),
            "return": _decision_json(store.can_return(order["order_id"])),
        },
    }


def _decision_json(decision):
    return {"allowed": bool(decision.allowed), "reason": decision.reason,
            "alternative": decision.alternative}


def trace_json(trace):
    """Tool 실행 기록을 화면이 읽는 모양으로.

    상담창에서 어떤 Tool 을 어떤 인자로 불렀는지 펼쳐 보는 부분입니다.
    """
    rows = []
    for entry in trace or []:
        result = entry.get("result") or {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        row = {
            "name": entry.get("tool"),
            "args": entry.get("arguments") or {},
            "ok": bool(result.get("success", True)),
            "msg": result.get("message", ""),
        }
        # 실패에도 종류가 있다. 사용자에게 물어야 하는 것(needs_input: 성별·사진 속 품목·사이즈)이나
        # 결과 없음(no_match)·규칙상 불가(blocked)는 오류가 아니다. 화면이 이를 "실패" 로 세지 않도록
        # tools.fail() 의 status 를 함께 보낸다.
        if not row["ok"]:
            row["status"] = result.get("status") or "failed"
        # 사진 검색은 무엇을 봤고, 무엇을 기준으로, 어떤 질의로 찾았는지를 화면에 보이도록
        # query plan 요약을 함께 보낸다. 다른 툴은 이 키가 없다.
        # 경로가 둘이라 모양도 둘이다 (tools.search_by_image_and_text):
        #   Qwen3-VL 통합 경로 → route·retrieval_query_en·hard_filters (query_plan 없음)
        #   SigLIP+KURE RRF    → query_plan (Qwen 이 실패했거나 설정이 rrf 일 때)
        plan = data.get("query_plan")
        if entry.get("tool") == "search_by_image_and_text" and data.get("route") \
                and not isinstance(plan, dict):
            row["plan"] = {
                "route": data.get("route"),
                "visual_summary": data.get("visual_summary"),
                "retrieval_query_en": data.get("retrieval_query_en"),
                "user_filters": data.get("hard_filters") or {},
                "unapplied_soft_filters": data.get("unapplied_soft_filters") or {},
                "negated_filters": data.get("negated_filters") or {},
                "analysis_cached": data.get("analysis_cached"),
            }
        elif isinstance(plan, dict):
            row["plan"] = {
                "route": data.get("ranking") or "rrf_siglip_kure",
                # 설정은 통합 경로인데 이 모양이 왔다 = Qwen 이 실패해서 내려온 것
                "fallback": config.MULTIMODAL_BACKEND != "rrf",
                "negated_filters": data.get("negated_filters") or {},
                "visual_summary": data.get("visual_summary"),
                "visual_retrieval_query": plan.get("visual_retrieval_query"),
                "semantic_retrieval_query": plan.get("semantic_retrieval_query"),
                "reference_filters": data.get("reference_filters") or {},
                "reference_filters_relaxed": data.get("reference_filters_relaxed") or [],
                "user_filters": data.get("user_filters") or {},
                "visual_analysis_available": data.get("visual_analysis_available"),
                "analysis_cached": data.get("analysis_cached"),
            }
        rows.append(row)
    return rows


def pending_json(agent):
    """확인 대기를 화면 버튼이 읽는 모양으로. 없으면 None.

    arguments 는 보내지 않습니다. 화면이 필요한 것은 무엇을 승인하는지(label)와
    승인할 때 돌려보낼 열쇠(key)뿐이고, 인자는 앱이 들고 있어야 합니다.
    모델도 화면도 인자를 만들어 보낼 수 없어야 승인이 열쇠로만 성립합니다.

    expires_in 은 남은 초입니다. 화면이 그 시간이 지나면 버튼을 거두고
    "확인 시간이 지났습니다" 를 띄웁니다. 서버도 approve 에서 같은 시각으로 거절합니다.
    """
    pending = getattr(agent, "pending", None)
    if pending is None:
        return None
    if pending.expired():
        agent.pending = None
        agent.postponed = None
        return None
    return {
        "expires_in": max(0, int(pending.seconds_left())),
        "items": [
            {"key": item["key"], "label": item["label"]}
            for item in pending.items
        ],
    }


def chat_json(session, answer, trace):
    """대화 응답. 검색 상품은 채팅 카드가 아니라 메인 그리드로 보낸다."""
    return {
        "reply": answer,
        "trace": trace_json(trace),
        # 채팅 미니 카드용 "products" 는 없앴다. 화면은 search_results 만 그리드에 그린다.
        "search_results": search_results_from_trace(session.store, trace),
        "search_performed": search_performed(trace),
        "search_note": search_note(trace),
        "pending": pending_json(session.agent),
    }


# 화면의 상품 그리드를 채우는 Tool. 상품 목록을 돌려주는 툴은 전부 여기다 — 글 검색, 사진 검색,
# 대체 상품 추천, 비교. 채팅 안 미니 카드는 쓰지 않는다(사용자 결정 2026-09-22): 상품은 항상
# 그리드 한 곳에서 본다. 장바구니·주문처럼 상품 ID가 부수적으로 들어 있는 툴은 넣지 않는다 —
# "담아줘" 한 마디에 그리드가 그 상품 하나로 바뀌면 안 된다.
GRID_SEARCH_TOOLS = ("search_product", "search_by_image_and_text",
                     "recommend_similar_products", "comparing_info")


def search_note(trace):
    """검색 결과에 붙일 한 줄 안내. 기준 통과가 적어 비슷한 상품을 채운 경우에만."""
    for entry in reversed(trace or []):
        if entry.get("tool") not in GRID_SEARCH_TOOLS:
            continue
        result = entry.get("result") or {}
        data = result.get("data") or {}
        if result.get("success") and isinstance(data, dict) and data.get("backfilled"):
            qualified = data.get("qualified", 0)
            if qualified:
                return (f"딱 맞는 상품은 {qualified}개입니다. 비슷한 상품 "
                        f"{data['backfilled']}개를 함께 보여드려요.")
            return f"딱 맞는 상품은 없어서 비슷한 상품 {data['backfilled']}개를 보여드려요."
        return None
    return None


def search_performed(trace):
    """성공한 검색이 0건이어도 화면이 빈 검색 결과를 표시할 수 있게 한다."""
    return any(
        entry.get("tool") in GRID_SEARCH_TOOLS
        and (entry.get("result") or {}).get("success")
        for entry in (trace or [])
    )


def search_results_from_trace(store, trace):
    """마지막으로 성공한 검색(글·사진) 결과를 화면용 상품 객체로, Tool 이 준 순서대로 반환한다."""
    for entry in reversed(trace or []):
        if entry.get("tool") not in GRID_SEARCH_TOOLS:
            continue
        result = entry.get("result") or {}
        if not result.get("success"):
            continue
        data = result.get("data") or {}
        # search·recommend 는 {"products": [...]}, comparing_info 는 행 목록을 그대로 돌려준다.
        rows = data.get("products") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            continue
        found = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            product = store.get_product(row.get("product_id") or row.get("id"))
            if product is not None:
                found.append(product_json(store, product))
        attach_wishes(store, found)
        return found[:MAX_SEARCH_RESULTS]
    return []


# --------------------------------------------------------------------------
# 상품
# --------------------------------------------------------------------------

_SORTS = {
    "price_asc": (lambda p: p["price"], False),
    "price_desc": (lambda p: p["price"], True),
    "rating": (lambda p: p["rating"], True),
    "review": (lambda p: p["review_count"], True),
    # recommend 는 여기 없습니다. 채팅 검색(SQL 기본 정렬)과 같은 식을 써야 하므로
    # store.sort_products(rows, "recommend") (베이지안 평균) 로 정렬합니다.
}


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _size_or_none(value):
    """사이즈는 FREE/S/M/L/235/32처럼 숫자와 문자를 모두 허용한다."""
    if value in (None, "") or isinstance(value, bool):
        return None
    return str(value).strip().upper() or None


async def api_products(request):
    store = session_of(request).store
    q = request.query_params
    # KURE 인코딩·전체 정렬은 수백 ms 걸리는 동기 작업이다. 이벤트 루프에서 돌리면
    # 그동안 다른 사용자 요청까지 서버 전체가 멈춘다. 스레드풀에서 돌린다.
    # 세션 락은 잡지 않는다 — 잡으면 상담 턴(LLM 호출 중)이 끝날 때까지 상품 목록이 멈춘다.
    # 대신 Store 연결의 db_lock 만 잡는다 (상담이 Tool 을 실행하는 동안만 겹친다).
    outcome = await _store_call(store, _products_page, store, q)
    if isinstance(outcome, JSONResponse):
        return outcome
    payload, rows, total_rows, semantic_pool = outcome
    lineage = await run_in_threadpool(
        events.record_impressions, DSN,
        user_id=request.state.user_id,
        results=[{"product_id": p["id"]} for p in rows],
        source="product_search",
        model_name=config.EMBED_MODEL_NAME if semantic_pool else "sql_rules",
        model_version=config.EMBED_MODEL_NAME if semantic_pool else "v1")
    return JSONResponse(payload, headers={
        "X-Recommendation-ID": lineage["recommendation_id"],
        "X-Total-Count": str(total_rows),
    })


def _products_page(store, q):
    """상품 목록 한 페이지. (payload, 페이지 행, 전체 행 수, 의미검색 여부) 또는 오류 응답."""
    semantic_query = (q.get("semanticQuery") or "").strip() or None
    product_name = (q.get("productName") or "").strip() or None
    # 헤더 검색창의 글(query). 구조화 조건은 SQL, 자연어는 임베딩으로 나눈다:
    #   분류 이름과 정확히 같으면("운동화", "신발") → 분류 필터 (전부, 50개 상한 없음)
    #   브랜드 이름과 정확히 같으면("nike")      → 브랜드 필터
    #   그 밖의 글                                → 상담 검색과 같은 의미 검색(KURE)
    typed = {}
    text = (q.get("query") or "").strip()
    if text and not (semantic_query or product_name):
        if text in store.available_groups():
            typed["group"] = text
        elif text in store.available_categories():
            typed["category"] = text
        elif (brand := store.resolve_brand(text)) is not None:
            typed["brand"] = brand
        else:
            semantic_query = text
    filters = {
        "product_name": product_name,
        # 분류 이름을 직접 쳤으면 그쪽이 우선이다. "상의" 를 보던 중 "운동화" 를 치면
        # 화면의 group=상의 와 겹쳐 0건이 되던 것을 막는다 (소분류가 대분류를 정한다).
        "group": typed.get("group") or (None if "category" in typed else q.get("group") or None),
        "category": typed.get("category") or (None if "group" in typed else q.get("category") or None),
        "gender": q.get("gender") or None,
        "color": q.get("color") or None,
        "brand": q.get("brand") or typed.get("brand"),
        "material": q.get("material") or None,
        "min_price": _int_or_none(q.get("minPrice")),
        "max_price": _int_or_none(q.get("maxPrice")),
        "size": _size_or_none(q.get("size")),
        "machine_washable": True if q.get("machineWashable") in ("1", "true") else None,
        # search_product 의 in_stock 과 같은 조건. 상담 검색("재고 있는 것만") 뒤 화면이
        # 정렬·필터를 바꿔 다시 부를 때 이 조건이 빠지면 품절 상품이 섞여 나온다.
        "in_stock": True if q.get("inStock") in ("1", "true") else None,
    }
    filters = {k: v for k, v in filters.items() if v is not None}

    minimum = filters.get("min_price")
    maximum = filters.get("max_price")
    if minimum is not None and minimum < 0 or maximum is not None and maximum < 0:
        return JSONResponse({"error": "가격은 0원 이상이어야 합니다"}, status_code=400)
    if minimum is not None and maximum is not None and minimum > maximum:
        return JSONResponse(
            {"error": "최소 가격은 최대 가격보다 클 수 없습니다"}, status_code=400)

    # 정렬은 여기서 합니다. store 의 sort 는 4종이고 화면은 recommend 도
    # 쓰기 때문입니다. limit 을 크게 줘서 전체를 받고 정렬합니다 —
    # count_products 가 쓰는 방식과 같습니다.
    sort = q.get("sort") or "recommend"
    semantic_pool = False
    # Tool과 동일하게 SQL 필터 -> 의미 상위 50개 -> 명시 정렬 순서로 처리한다.
    # 가격 정렬이 함께 와도 사용자의 의미 조건을 버리지 않는다.
    if semantic_query:
        semantic = store.search_semantic_result(
            semantic_query, top_k=MAX_SEARCH_RESULTS,
            min_score=config.SEMANTIC_MIN_SCORE,
            min_results=config.SEMANTIC_MIN_RESULTS,
            **filters)
        rows = semantic["products"]
        if semantic["available"]:
            semantic_pool = True
            if sort in ("price_asc", "price_desc", "rating", "review"):
                rows = store.sort_products(rows, sort)
        else:
            # 임베딩을 쓸 수 없는 환경(st 미설치 등)이면 Tool 과 같이 SQL 결과로
            # 되돌아갑니다. 예전에는 여기서 빈 배열이 나가 "조건에 맞는 상품이
            # 없습니다" 가 떴는데, 실제로는 조건에 맞는 상품이 있는 상태였습니다.
            rows = store.search_products(limit=10 ** 9, **filters)
    else:
        rows = store.search_products(limit=10 ** 9, **filters)

    # 의미 검색 결과의 순서가 곧 추천 순서입니다. 여기에 recommend(베이지안 평균)
    # 를 다시 걸면 첫 화면(Tool 결과 순서)과 칩 하나 토글한 뒤의 순서가
    # 달라집니다. 사용자가 가격·평점 정렬을 직접 고른 경우에만 다시 정렬합니다.
    if sort == "wish":
        # 찜 많은순 = 합성 시작값 + 실제 찜 (v13). 정렬 목록의 한 항목 (첫 화면 기본은 추천순).
        # 의미 검색 결과도 같은 기준으로 다시 줄 세운다. 동률은 product_id 로 고정.
        real = db.wish_counts(store.conn)
        rows = sorted(rows, key=lambda p: (-((p.get("wish_base") or 0) + real.get(p["id"], 0)),
                                           p["id"]))
        key = None
    elif semantic_pool:
        key = None
    elif sort == "recommend":
        key = None
        rows = store.sort_products(rows, "recommend")
    else:
        key = _SORTS.get(sort)
    if key is not None:
        # 동률에서 순서가 흔들리지 않게 product_id 를 타이브레이커로 둡니다.
        # 없으면 같은 요청에 다른 순서가 나올 수 있습니다.
        rows = sorted(rows, key=lambda p: p["id"])
        rows = sorted(rows, key=key[0], reverse=key[1])

    if sort == "recommend":
        # 추천순에서만 품절 상품을 맨 뒤로 보낸다. 가격·평점 등 사용자가 고른 정렬은 그대로 둔다.
        rows = store.in_stock_first(rows)

    total_rows = len(rows)
    offset = _int_or_none(q.get("offset")) or 0
    page_size = _int_or_none(q.get("limit")) or 60
    if offset < 0 or not 1 <= page_size <= 100:
        return JSONResponse(
            {"error": "offset은 0 이상, limit은 1~100이어야 합니다"}, status_code=400)
    rows = rows[offset:offset + page_size]
    payload = attach_wishes(store, [product_json(store, p) for p in rows])
    return payload, rows, total_rows, semantic_pool


async def api_product(request):
    store = session_of(request).store
    product = store.get_product(request.path_params["product_id"])
    if product is None:
        return JSONResponse({"error": "상품을 찾을 수 없습니다"}, status_code=404)
    recommendation_id = request.headers.get("x-recommendation-id")
    try:
        await run_in_threadpool(
            events.record_interaction, DSN,
            event_type="product_view", user_id=request.state.user_id,
            product_id=product["id"], recommendation_id=recommendation_id,
            source="product_detail")
    except ValueError:
        return JSONResponse({"error": "추천 ID 형식이 올바르지 않습니다"}, status_code=400)
    return JSONResponse(await _store_call(
        store, lambda: attach_wishes(store, [product_json(store, product)])[0]))


# --------------------------------------------------------------------------
# 장바구니
#
# 화면의 버튼은 사용자가 직접 누른 것이므로 이미 확인입니다.
# 그래서 승인 게이트를 지나지 않고 store 를 바로 부릅니다.
# 대화로 지우는 경로만 /api/chat -> /api/approve 를 거칩니다.
# --------------------------------------------------------------------------

async def api_cart(request):
    store = session_of(request).store
    return JSONResponse(await _store_call(store, cart_json, store))


class _BadRequest(Exception):
    pass


def _cart_args(body, *, quantity_default=None):
    """장바구니 요청 본문에서 (product_id, size, quantity) 를 읽는다.

    키가 빠지거나 사이즈가 숫자가 아니면 KeyError/ValueError 로 500 이 나던 것을
    400 으로 바꿉니다. 화면이 잘못 보낸 것이지 서버가 죽을 일이 아닙니다.
    """
    try:
        pid = body["product_id"]
        size = _size_or_none(body["size"])
        if size is None:
            raise ValueError
        quantity = body.get("quantity", quantity_default)
        quantity = int(quantity) if quantity is not None else None
    except (KeyError, TypeError, ValueError):
        raise _BadRequest("product_id, size(FREE/S/M/L/숫자), quantity(정수) 가 필요합니다")
    # 색은 선택입니다. 안 보내면 Store 가 "색을 고르세요" 라고 답합니다
    # (색이 하나뿐인 상품은 그대로 담깁니다). 화면이 고르게 하는 것이
    # 맞지만, 안 골랐을 때 조용히 아무 색이나 담는 것보다는 거절이 낫습니다.
    color = body.get("color") or None
    if color is not None and not isinstance(color, str):
        raise _BadRequest("color 는 문자열이어야 합니다")
    return pid, size, color, quantity


def _bad_request(message):
    return JSONResponse({"ok": False, "message": message}, status_code=400)


async def api_cart_add(request):
    store = session_of(request).store
    body = await request.json()
    try:
        pid, size, color, quantity = _cart_args(body, quantity_default=1)
    except _BadRequest as problem:
        return _bad_request(str(problem))
    def work():
        ok, message = store.add_to_cart(pid, size, color, quantity)
        return {"ok": ok, "message": screen_message(store, pid, size, message),
                "cart": cart_json(store)}
    return JSONResponse(await _store_call(store, work))


async def api_cart_patch(request):
    store = session_of(request).store
    body = await request.json()
    try:
        pid, size, color, want = _cart_args(body)
    except _BadRequest as problem:
        return _bad_request(str(problem))
    if want is None:
        return _bad_request("quantity 가 필요합니다")

    def work():
        # 수량을 맞출 대상은 (상품, 색, 사이즈) 한 줄입니다. 색을 안 보내면
        # 같은 상품·사이즈의 다른 색 줄과 섞여서 엉뚱한 줄이 늘어납니다.
        # 현재 수량 읽기와 바꾸기를 한 잠금 안에서 해야 그 사이 상담이 끼어들지 않는다.
        current = next(
            (i["quantity"] for i in store.view_cart()["items"]
             if i["product_id"] == pid and i["size"] == size
             and (color is None or i.get("color") == color)),
            0,
        )
        if want <= 0:
            ok, message = store.remove_from_cart(pid, size, color)
        elif want < current:
            ok, message = store.remove_from_cart(pid, size, color, current - want)
        elif want > current:
            ok, message = store.add_to_cart(pid, size, color, want - current)
        else:
            ok, message = True, "변경 없음"
        return {"ok": ok, "message": screen_message(store, pid, size, message),
                "cart": cart_json(store)}
    return JSONResponse(await _store_call(store, work))


async def api_cart_delete(request):
    store = session_of(request).store
    body = await request.json()
    try:
        pid, size, color, _ = _cart_args(body)
    except _BadRequest as problem:
        return _bad_request(str(problem))
    def work():
        ok, message = store.remove_from_cart(pid, size, color)
        return {"ok": ok, "message": screen_message(store, pid, size, message),
                "cart": cart_json(store)}
    return JSONResponse(await _store_call(store, work))


async def api_checkout(request):
    store = session_of(request).store
    body = await request.json() if await request.body() else {}
    items = body.get("items") or None
    selection = None
    if items:
        selection = [
            {"product_id": i["product_id"], "size": _size_or_none(i["size"]),
             "color": i.get("color") or None}
            for i in items
        ]
    def work():
        # 결제는 여러 문장을 한 트랜잭션으로 묶는다(재고 차감 → 주문 → 장바구니 비우기).
        # db_lock 안이라 상담의 Tool 이 그 사이에 같은 연결로 commit 하지 못한다.
        ok, message, created = store.checkout(selection)
        return {
            "ok": ok,
            "message": message,
            "total": sum(o["price"] for o in created),
            "cart": cart_json(store),
        }
    return JSONResponse(await _store_call(store, work))


async def api_orders(request):
    store = session_of(request).store
    # 최신 주문이 위로 옵니다. 화면이 그 순서를 기대하고 (mock 은 unshift),
    # 방금 주문한 것이 목록 맨 아래에 있으면 사용자가 못 찾습니다.
    # 같은 날 주문이 여럿이면 주문번호 역순 — 번호가 발급 순서입니다.
    def work():
        rows = sorted(
            store.orders,
            key=lambda o: (o["ordered_at"], o["order_id"]),
            reverse=True,
        )
        return [order_json(store, o) for o in rows]
    return JSONResponse(await _store_call(store, work))


# --------------------------------------------------------------------------
# 대화와 승인
# --------------------------------------------------------------------------

def _preanalyze_image(user_id, query_image_id):
    """첨부 사진을 VLM으로 먼저 이해하고, 검증 블록에 넣을 줄들을 만든다.

    반환 예:
      analysis_id=…  (분석 3건 캐시)
      사진 이해(VLM 추정값, 하드 필터 금지): item_1=니트 · 초록색 · 무지 · 오버핏 "연두색 …"; item_2=모자 · 검은색
    실패하면 analysis_id 줄 대신 실패 사유 한 줄. 검색 툴이 다시 시도하고, 안 되면 SigLIP만으로 간다.
    """
    import requests
    try:
        analysis = photo_analysis.analyze(user_id, query_image_id)
    except (photo_analysis.VLMAnalysisError, requests.RequestException,
            photo_store.ImageQueryError) as error:
        return [f"사진 분석 실패: {type(error).__name__}. analysis_id 없이 검색한다."]
    items = analysis.get("items") or []
    described = []
    for item in items[:4]:
        summary = photo_analysis.summarize_item(item) or "종류 미상"
        caption = (item.get("visual_description") or "").strip()
        if caption:
            summary += f' "{caption[:80]}"'
        features = item.get("search_features_en") or {}
        if features:
            summary += " search_features_en=" + json.dumps(features, ensure_ascii=False)
        described.append(f"{item.get('item_id')}={summary}")
    # ID 줄은 값만 둔다. 뒤에 무엇이든 붙이면 모델이 그대로 인자에 복사해 36자 검증에 걸린다.
    lines = [f"analysis_id={analysis['analysis_id']}"]
    if described:
        cached = " (같은 사진의 이전 분석 재사용)" if analysis.get("analysis_cached") else ""
        lines.append("사진 이해(VLM 추정값, 하드 필터 금지)" + cached + ": " + "; ".join(described))
        if len(items) > 1:
            lines.append(f"아이템 {len(items)}개. 묻지 말고 먼저 search_by_image_and_text 를 부른다. "
                         "어느 아이템으로 찾을지는 툴이 크기를 보고 정한다(큰 것은 바로 진행, 비슷하면 툴이 선택을 요청).")
    else:
        seen = ", ".join(analysis.get("other_objects") or [])
        lines.append("사진 이해: 검색할 패션 아이템이 없다" + (f"(보이는 것: {seen})" if seen else "")
                     + ". 이 쇼핑몰은 의류·신발·가방·모자·액세서리만 판다.")
    return lines


async def api_chat(request):
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message 가 비어 있습니다"}, status_code=400)

    query_image_id = body.get("query_image_id")
    agent_image_data_url = None
    if query_image_id is not None:
        try:
            record = await run_in_threadpool(
                photo_store.get_query_record,
                request.state.user_id, query_image_id)
        except photo_store.ImageQueryError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        # 사진은 에이전트가 툴을 고르기 전에 서버가 먼저 이해한다(같은 사진은 캐시).
        # 그래야 어떤 툴을 고르든 VLM은 정확히 한 번 돌고, 모델은 사진 내용을 알고
        # 인자를 채운다. VLM이 죽어도 대화는 계속되어야 하므로 실패는 한 줄로만 남긴다.
        analysis_lines = await run_in_threadpool(
            _preanalyze_image, request.state.user_id, record["query_id"])
        # 툴 선택과 retrieval_query_en 생성도 실제 픽셀을 근거로 하게 한다. 분석 요약은
        # 캐시/아이템 선택용이며, 메인 Gemma가 사진을 봤다고 가장하지 않는다.
        agent_image = await run_in_threadpool(
            photo_store.load_query_image,
            request.state.user_id, record["query_id"])
        agent_image_data_url = photo_analysis.data_url(agent_image)
        # UUID는 세션 DB에서 소유권·만료를 확인한 사실이다. 사용자가 쓴 자연어와
        # 구분해 모델이 이미지 속 텍스트나 임의 ID를 명령으로 해석하지 않게 한다.
        agent_message = (
            f"{message}\n\n[검증된 첨부 이미지]\n"
            f"query_image_id={record['query_id']}\n"
            + "\n".join(analysis_lines) + "\n"
            "사진 검색은 search_by_image_and_text 하나만 사용하고 위 ID들을 그대로 넘긴다. "
            "사진 이해 값은 VLM 추정이므로 사용자가 직접 말한 조건만 구조화 인자에 넣는다. "
            "이 ID들을 추측하거나 바꾸지 않는다."
        )
    else:
        agent_message = message

    session = session_of(request)

    def turn():
        answer, trace = session.agent.run(
            agent_message, session.history, image_data_url=agent_image_data_url)
        session.history.append({"role": "user", "content": agent_message})
        # 이번 턴에 부른 툴(인자 + 결과 요약)을 최종 답 앞에 남긴다. 없으면 다음 턴 모델이 앱이 만든
        # 결과 목록을 "툴 없이 쓴 답"으로 보고 흉내 낸다(agent.tool_history_messages).
        session.history.extend(tool_history_messages(trace))
        session.history.append({"role": "assistant", "content": answer})
        trim_history(session.history)
        return chat_json(session, answer, trace)

    return JSONResponse(await run_in_threadpool(_with_session, session, turn))


async def api_approve(request):
    body = await request.json() if await request.body() else {}
    keys = body.get("keys")
    session = session_of(request)

    def turn():
        # approve 는 모델을 부르지 않지만 Tool 을 실행하고 미리보기를 다시
        # 계산합니다. run 과 같은 락 안에서 돌아야 순서가 보장됩니다.
        answer, trace = session.agent.approve(keys)
        session.history.extend(tool_history_messages(trace))
        session.history.append({"role": "assistant", "content": answer})
        # 승인 버튼으로 끊긴 요청에 남은 부탁이 있으면 이어서 진행한다.
        more = session.agent.continue_after_approval(session.history)
        if more is not None:
            extra, extra_trace = more
            answer = f"{answer}\n\n{extra}".strip()
            trace = list(trace) + list(extra_trace)
            session.history.extend(tool_history_messages(extra_trace))
            session.history.append({"role": "assistant", "content": extra})
        trim_history(session.history)
        return chat_json(session, answer, trace)

    return JSONResponse(await run_in_threadpool(_with_session, session, turn))


async def api_order_action(request):
    """주문 화면의 [주문 취소]·[반품 신청] 버튼.

    바로 실행하지 않는다. 채팅으로 "취소해줘" 라고 했을 때와 **같은 확인 대기**를
    연다 — 미리보기를 계산해 PendingAction 을 만들고, 사용자는 상담창의 승인
    버튼으로 실행한다. 되돌릴 수 없는 작업의 경로가 하나뿐이어야 승인 규칙(열쇠·
    만료·스냅샷 비교)이 화면 버튼에도 똑같이 적용된다. 모델은 부르지 않는다.
    """
    order_id = request.path_params["order_id"]
    body = await request.json() if await request.body() else {}
    kind = (body.get("kind") or "").strip()
    tool = {"cancel": "cancel_order", "return": "return_order"}.get(kind)
    if tool is None:
        return JSONResponse({"error": "kind 는 cancel 또는 return 이어야 합니다"}, status_code=400)
    arguments = {"order_id": order_id}
    if tool == "return_order":
        arguments["reason"] = (body.get("reason") or "").strip() or "고객 요청"
    session = session_of(request)

    def turn():
        agent = session.agent
        if agent.pending is not None:
            # 이미 다른 확인이 떠 있으면 섞지 않는다. 먼저 그것을 끝내게 한다.
            return chat_json(session, "먼저 상담창에 떠 있는 확인을 승인하거나 거절해 주세요.", [])
        agent.turn += 1
        trace = []
        summary, problems = agent._open_pending([{"tool": tool, "arguments": arguments}], trace)
        if summary:
            answer = summary
        else:
            answer = "지금은 처리할 수 없습니다.\n  " + "\n  ".join(problems or [tool])
        session.history.append({"role": "assistant", "content": answer})
        return chat_json(session, answer, trace)

    return JSONResponse(await run_in_threadpool(_with_session, session, turn))


async def api_reject(request):
    session = session_of(request)

    def turn():
        # pending 만 지우면 미뤄 둔 후속 작업(postponed)이 남아서, 나중에 다른
        # 승인을 할 때 "이어서 부탁하신 작업입니다" 로 되살아납니다.
        # agent.reject() 가 둘 다 정리하고, 이미 실행된 것은 그대로임을 말합니다.
        answer, trace = session.agent.reject()
        session.history.append({"role": "assistant", "content": answer})
        return chat_json(session, answer, trace)

    return JSONResponse(await run_in_threadpool(_with_session, session, turn))


# 화면에 다시 그릴 대화에서 떼어 낼 부분. 사진 첨부 턴은 서버가 모델용 안내를
# 사용자 문장 뒤에 붙여 history 에 넣는다 (api_chat). 사람에게는 원문만 보인다.
_IMAGE_NOTE = "\n\n[검증된 첨부 이미지]"


HISTORY_TURNS = 10


def trim_history(history, turns=HISTORY_TURNS):
    """최근 turns 개 사용자 턴만 남긴다. 사용자 말에서 끊어야 툴 호출과 그 결과가 갈라지지 않는다.

    예전에는 메시지 20개로 잘랐는데, 툴 기록이 들어가면 tool_calls 와 tool 결과 사이가 잘려
    짝 없는 tool 메시지가 맨 앞에 남을 수 있다(모델 서버가 거부한다).
    """
    starts = [index for index, item in enumerate(history) if item.get("role") == "user"]
    if len(starts) > turns:
        del history[:starts[-turns]]


def history_json(history):
    """세션 대화 내역을 화면 말풍선 모양으로. 모델용 툴 기록(tool_calls · tool)은 그리지 않는다."""
    rows = []
    for item in history or []:
        role, text = item.get("role"), item.get("content")
        if role not in ("user", "assistant") or not isinstance(text, str) or item.get("tool_calls"):
            continue
        if role == "user" and _IMAGE_NOTE in text:
            text = "📎 사진 첨부\n" + text.split(_IMAGE_NOTE, 1)[0]
        rows.append({"role": "me" if role == "user" else "bot", "text": text})
    return rows


async def api_session(request):
    """새로고침한 화면이 이어서 그릴 상태: 대화 내역 · 확인 대기 · 계획.

    확인 대기와 대화는 세션 DB 에 남아 있는데 화면은 메모리에서 시작한다.
    이것을 다시 읽지 않으면 새로고침 뒤 승인 버튼이 사라지고, 서버는 "먼저 떠 있는
    확인을 끝내라" 며 주문 취소·반품을 막는데 사용자에게는 끝낼 버튼이 없다.
    """
    session = session_of(request)

    def read():
        with session.lock:
            had_pending = session.agent.pending is not None
            pending = pending_json(session.agent)
            if had_pending and session.agent.pending is None:
                session.save()          # 읽다가 만료를 발견했으면 바로 반영 (api_pending 과 같다)
            return {"messages": history_json(session.history),
                    "pending": pending}

    return JSONResponse(await run_in_threadpool(read))


async def api_pending(request):
    session = session_of(request)

    def read():
        had_pending = session.agent.pending is not None
        result = pending_json(session.agent)
        # 조회하는 순간 만료를 발견했다면 DB 에도 즉시 반영한다. 그렇지 않으면
        # 재시작 때마다 오래된 JSON 을 다시 읽고 버리는 불필요한 상태가 남는다.
        if had_pending and session.agent.pending is None:
            session.save()
        return result

    def locked_read():
        with session.lock:
            return read()

    # 같은 사용자의 모델 호출이 길어져도 HTTP 이벤트 루프 전체를 막지 않는다.
    return JSONResponse(await run_in_threadpool(locked_read))


async def _read_upload(request):
    """Content-Length가 없거나 거짓이어도 상한을 넘는 순간 읽기를 중단한다."""
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError as error:
            raise photo_store.ImageQueryError(
                "Content-Length가 올바르지 않습니다.") from error
        if declared_size > config.IMAGE_QUERY_MAX_BYTES:
            raise photo_store.ImageQueryError("업로드 이미지가 너무 큽니다.")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > config.IMAGE_QUERY_MAX_BYTES:
            raise photo_store.ImageQueryError("업로드 이미지가 너무 큽니다.")
    return bytes(data)


async def api_image_upload(request):
    """원본 이미지를 검증·정규화하고 소유권이 묶인 임시 ID를 발급한다."""
    try:
        raw = await _read_upload(request)
        result = await run_in_threadpool(
            photo_store.store_query, request.state.user_id, raw,
            request.headers.get("content-type", ""))
        return JSONResponse(result, status_code=201)
    except photo_store.ImageQueryError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:
        return JSONResponse(
            {"error": f"이미지 저장 중 오류가 발생했습니다: {type(error).__name__}"},
            status_code=503)


async def api_meta(request):
    store = session_of(request).store
    return JSONResponse({
        # 화면의 카테고리 네비가 쓴다. 하드코딩하면 카탈로그가 바뀔 때마다 화면이 뒤처진다 —
        # 아마존으로 갈아끼우면서 원피스·이너가 사라지고 가방·모자·기타가 생겼다.
        "category_groups": {g: store.categories_in_group(g) for g in store.available_groups()},
        "colors": store.available_colors(),
        "brands": store.available_brands(),
    })


# --------------------------------------------------------------------------
# 상품 사진 중계
# --------------------------------------------------------------------------
# 브라우저가 MinIO(127.0.0.1:9000)를 직접 보게 하지 않는다. 그러려면 CORS 와
# 서명 URL 이 필요하고, 시연에서는 그게 배울 것이 없는 종류의 일이다.
# 서버가 읽어서 내려준다. 나중에 presigned URL 로 바꾸면 이 라우트만 빠진다.
#
# 버킷은 카탈로그 버킷 하나뿐이고, 그 안에 상품 사진(products/…)과 사용자가 올린
# 질의 사진(query-images/…)이 **같이** 있다. 그래서 버킷만 검사하면 안 되고 키 접두어를
# 봐야 한다 — 상품 사진만 열어 준다. 질의 사진은 올린 사람의 세션 안에서만 쓰인다.

_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp"}
_MEDIA_PUBLIC_PREFIX = "products/"        # 이 아래만 공개. query-images/ 는 안 된다


def _read_media(bucket, key):
    from minio.error import S3Error
    client = photo_store.minio_client()
    try:
        response = client.get_object(bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as error:
        if error.code in ("NoSuchKey", "NoSuchBucket"):
            return None
        raise


async def api_media(request):
    bucket = request.path_params["bucket"]
    key = request.path_params["key"]
    if (bucket != config.MINIO_BUCKET or ".." in key or key.startswith("/")
            or not key.startswith(_MEDIA_PUBLIC_PREFIX)):
        return Response(status_code=404)
    try:
        body = await run_in_threadpool(_read_media, bucket, key)
    except Exception as error:                       # MinIO 가 꺼져 있으면 여기
        return JSONResponse({"error": f"사진 저장소에 닿지 못했습니다: {type(error).__name__}"},
                            status_code=502)
    if body is None:
        return Response(status_code=404)
    suffix = key[key.rfind("."):].lower() if "." in key else ""
    return Response(body, media_type=_MEDIA_TYPES.get(suffix, "image/jpeg"),
                    # 키에 sha 가 들어가 내용이 바뀌면 키도 바뀐다. 오래 캐시해도 된다.
                    headers={"Cache-Control": "public, max-age=86400, immutable"})


# --------------------------------------------------------------------------
# 찜 — 사용자별로 wishlists 에 두고 상품의 찜 수를 센다.
# --------------------------------------------------------------------------

async def api_wishlist(request):
    store = session_of(request).store
    return JSONResponse({"product_ids": await _store_call(
        store, db.user_wishlist, store.conn, store.user_id)})


async def _set_wish(request, on):
    store = session_of(request).store
    body = await request.json() if await request.body() else {}
    product_id = body.get("product_id")
    if not isinstance(product_id, str) or store.get_product(product_id) is None:
        return JSONResponse({"ok": False, "message": "상품을 찾을 수 없습니다"}, status_code=404)
    def work():
        db.set_wish(store.conn, store.user_id, product_id, on)
        item = attach_wishes(store, [{"id": product_id}])[0]
        return {"wished": item["wished"], "wish_count": item["wish_count"]}
    return JSONResponse(await _store_call(store, work))


async def api_wish_add(request):
    return await _set_wish(request, True)


async def api_wish_remove(request):
    return await _set_wish(request, False)


# --------------------------------------------------------------------------
# 데모 사용자 전환
# --------------------------------------------------------------------------
# 시연용 사용자 info1~info5 (config.DEMO_USERS) 중 하나로 이 브라우저를 옮긴다.
# 비밀번호가 없는 **데모용 사용자 선택**이다. 로그인이 아니며, 목록 밖의 id 로는 옮길 수 없다
# (클라이언트가 보낸 값을 user_id 로 받아들이지 않는다는 SessionMiddleware 원칙을 지킨다).
# 전환은 새 세션 토큰을 발급하고 이전 토큰을 폐기한다. 처음 쓰는 데모 사용자는
# get_session 이 사용자 행을 만들고 데모 주문을 심는다(새 사용자와 같다).

def _session_cookie(response, token):
    response.set_cookie(SESSION_COOKIE, token, max_age=config.SESSION_MAX_AGE_SECONDS,
                        httponly=True, samesite="lax", secure=config.SESSION_COOKIE_SECURE)


async def api_demo_users(request):
    current = request.state.user_id
    return JSONResponse({
        "users": list(config.DEMO_USERS),
        "current": current,
        "is_demo": current in config.DEMO_USERS,
    })


async def api_switch_user(request):
    body = await request.json() if await request.body() else {}
    target = body.get("user_id")          # 데모 사용자 id, 또는 null = 새 익명 사용자
    if target is not None and target not in config.DEMO_USERS:
        return JSONResponse({"error": "데모 사용자 목록에 없는 사용자입니다"}, status_code=400)
    old_token = request.cookies.get(SESSION_COOKIE)

    def switch():
        if target is None:
            return _issue_session()
        get_session(target)               # 처음이면 사용자 행 + 데모 주문
        with db.connect(DSN) as conn:
            token = db.create_web_session(conn, target, config.SESSION_MAX_AGE_SECONDS)
        return target, token

    user_id, token = await run_in_threadpool(switch)
    if old_token:
        def revoke():
            with db.connect(DSN) as conn:
                db.revoke_web_session(conn, old_token)
            with _session_cache_lock:
                _session_cache.pop(old_token, None)
        await run_in_threadpool(revoke)
    response = JSONResponse({"ok": True})
    _session_cookie(response, token)
    return response


async def api_reset(request):
    """현재 브라우저 사용자만 시연 시작 상태로 되돌린다."""
    session = session_of(request)

    def reset():
        # 같은 사용자의 진행 중인 대화가 끝난 뒤 초기화한다. Session 객체와 락은
        # 유지하고 내부 Store/Agent 만 새로 만들어 다른 요청이 낡은 객체를 잡지 않게 한다.
        # 연결을 닫고 Store 를 바꾸므로, 그 연결로 도는 화면 작업(_store_call)이 끝나기를 기다린다.
        with session.lock, session.store.db_lock:
            db.reset_user(session.store.conn, session.user_id)
            # 에이전트 상태는 업무 DB 가 아니라 세션 DB 에 있으므로 따로 지운다.
            # 안 지우면 되돌린 뒤에도 "아까 그거 담을까요?" 가 살아남는다.
            _session_state_delete(session.user_id)
            try:
                photo_store.delete_user_queries(session.user_id)
            except Exception as problem:
                print(f"[이미지 질의] 사용자 초기화 정리 실패: {problem}")
            session.close()
            session.store = Store(db_path=DSN, user_id=session.user_id)
            session.agent = ShoppingAgent(session.store)
            session.history = []

    await run_in_threadpool(reset)
    return JSONResponse({"ok": True, "message": "내 데이터를 처음 상태로 되돌렸습니다"})


routes = [
    Route("/api/meta", api_meta),
    Route("/api/products", api_products),
    Route("/api/products/{product_id}", api_product),
    Route("/api/cart", api_cart, methods=["GET"]),
    Route("/api/cart", api_cart_add, methods=["POST"]),
    Route("/api/cart", api_cart_patch, methods=["PATCH"]),
    Route("/api/cart", api_cart_delete, methods=["DELETE"]),
    Route("/api/checkout", api_checkout, methods=["POST"]),
    Route("/api/orders", api_orders),
    Route("/api/orders/{order_id}/action", api_order_action, methods=["POST"]),
    Route("/api/chat", api_chat, methods=["POST"]),
    Route("/api/approve", api_approve, methods=["POST"]),
    Route("/api/reject", api_reject, methods=["POST"]),
    Route("/api/pending", api_pending),
    Route("/api/session", api_session),
    Route("/api/demo-users", api_demo_users),
    Route("/api/wishlist", api_wishlist, methods=["GET"]),
    Route("/api/wishlist", api_wish_add, methods=["POST"]),
    Route("/api/wishlist", api_wish_remove, methods=["DELETE"]),
    Route("/api/switch-user", api_switch_user, methods=["POST"]),
    Route("/api/images", api_image_upload, methods=["POST"]),
    Route("/api/reset", api_reset, methods=["POST"]),
    Route("/media/{bucket}/{key:path}", api_media),
]

class NoCacheStaticFiles(StaticFiles):
    """화면 파일은 매번 서버에 확인하게 한다 (바뀌지 않았으면 304 라 비용이 작다).

    Cache-Control 이 없으면 브라우저가 Last-Modified 로 신선도를 추측해 옛 app.js 를
    그대로 쓴다. index.html 의 ?v= 는 사람이 올려야 하고, api.js·art.js 처럼 모듈이
    import 하는 파일에는 붙일 자리도 없다.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if WEB_DIR.exists():
    # 화면과 API 를 같은 서버에서 냅니다. 포트를 나누면 CORS 를 설정해야 하고,
    # 그건 이 프로젝트에서 배울 것이 없는 종류의 작업입니다.
    routes.append(Mount("/", app=NoCacheStaticFiles(directory=WEB_DIR, html=True)))

app = Starlette(routes=routes, middleware=[Middleware(SessionMiddleware)])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    # DB 가 준비되어 있는지 먼저 본다. 서버는 DB 를 만들지 않는다 — 스키마는 deploy/ 의 일이고,
    # 서버는 "준비 안 됨"을 분명히 말하고 멈추는 편이 낫다.
    if not db.looks_initialised(DSN):
        raise SystemExit(
            f"업무 DB 가 준비되지 않았습니다: {DSN}\n"
            f"  1) docker compose -f deploy/docker-compose.yml up -d\n"
            f"  2) deploy/README.md 대로 스키마를 적용하고 상품 데이터를 넣으세요.\n")

    # 만료된 것을 기동할 때 한 번 치우고, 그 뒤로도 주기적으로 치운다.
    if not purge_expired_once():
        print("세션 DB 에 붙지 못했습니다. 상태 저장이 꺼진 채로 뜹니다.")
    start_janitor()

    boot = get_session(db.DEMO_USER_ID)
    print(f"상품 {len(boot.store.products):,}개 · 기본 사용자 주문 {len(boot.store.orders)}건"
          f" · 확인 대기 만료 {config.PENDING_TTL_SECONDS}초")
    print(f"모델 {config.MODEL_NAME} · 사진 분석 {config.SHOPPING_VLM_MODEL}"
          + (f" · {config.MODEL_ENV_FILE}" if config.MODEL_ENV_FILE else ""))
    print(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
