/* ==========================================================================
   API 어댑터
   화면은 이 파일만 봅니다. 기본은 실서버, 주소에 ?mock 을 붙이면 mock.js 를 씁니다.

   서버 엔드포인트 (server.py)
     GET    /api/meta                 → {category_groups, ...}  카테고리 네비
     GET    /api/products?query&productName&semanticQuery&group&category&gender&color
                         &brand&material&minPrice&maxPrice&size&machineWashable&inStock&sort(wish·recommend·price_asc·price_desc·rating·review)&offset&limit
                                      → 상품 배열 (총 개수는 X-Total-Count 헤더)
     GET    /api/products/{id}
     GET    /api/cart
     POST   /api/cart                 {product_id, size, color, quantity}
     PATCH  /api/cart                 {product_id, size, color, quantity}
     DELETE /api/cart                 {product_id, size, color}
     POST   /api/checkout             {items?: [{product_id, size, color}]}   ← 선택 항목만 결제
     GET    /api/orders               → 주문 배열 (각 주문에 actions: 취소·반품 가능 여부)
     POST   /api/orders/{id}/action   {kind: cancel|return, reason?}  → 확인 대기만 연다
     POST   /api/images               raw JPEG/PNG/WebP → {query_image_id}
     POST   /api/chat                 {message, query_image_id?}
                                      → {reply, trace, search_results, search_performed,
                                         search_note, pending}   (plan 은 계획 모드 제거로 항상 null)
     POST   /api/approve              {keys: [...]}
     POST   /api/reject
     GET    /api/session              → {messages, pending}  새로고침 뒤 복원
     GET    /api/wishlist             → {product_ids}  내 찜
     POST   /api/wishlist             {product_id} 찜 / DELETE 같은 모양으로 해제 → {wished, wish_count}
     GET    /api/demo-users           → {users, current, is_demo}  데모 사용자 목록
     POST   /api/switch-user          {user_id | null}  데모 사용자로(null 이면 새 익명 사용자로) 전환
     POST   /api/reset                지금 사용자만 처음 상태로

   color 는 2026-09 에 늘었습니다. 같은 옷의 색이 상품 행이 아니라 variant 가
   되면서, 장바구니 한 줄의 열쇠가 (상품, 색, 사이즈) 가 되었습니다.
   ========================================================================== */
import { mock } from "./mock.js";

/* 기본은 실서버입니다. 서버 없이 화면만 보려면 ?mock 을 붙이세요.
   (http://127.0.0.1:8000/?mock) */
export const USE_MOCK = new URLSearchParams(location.search).has("mock");
const BASE = "/api";

let lastRecommendationId = null;

async function http(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (lastRecommendationId) headers["X-Recommendation-ID"] = lastRecommendationId;
  const res = await fetch(BASE + path, {
    method,
    headers: Object.keys(headers).length ? headers : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    // 서버가 {error} 또는 {ok:false, message} 로 이유를 보낸다 ("가격은 0원 이상이어야 합니다",
    // "재고가 18개뿐입니다"). 그 문장을 그대로 올려야 화면이 사람에게 이유를 말할 수 있다.
    const detail = await res.json().catch(() => null);
    const error = new Error(detail?.error || detail?.message || `${method} ${path} → ${res.status}`);
    error.status = res.status;
    throw error;
  }
  const recommendationId = res.headers.get("X-Recommendation-ID");
  if (recommendationId) lastRecommendationId = recommendationId;
  if (res.status === 204) return null;
  const data = await res.json();
  const total = Number(res.headers.get("X-Total-Count"));
  if (Array.isArray(data) && Number.isFinite(total)) {
    Object.defineProperty(data, "totalCount", { value: total, enumerable: false });
  }
  return data;
}

const real = {
  products: (o = {}) => http("/products?" + new URLSearchParams(
    Object.fromEntries(Object.entries(o).filter(([, v]) => v != null && v !== "")))),
  product: (id) => http(`/products/${id}`),
  cart: () => http("/cart"),
  addToCart: (product_id, size, color = null, quantity = 1) =>
    http("/cart", { method: "POST", body: { product_id, size, color, quantity } }),
  setQuantity: (product_id, size, color, quantity) =>
    http("/cart", { method: "PATCH", body: { product_id, size, color, quantity } }),
  removeFromCart: (product_id, size, color = null) =>
    http("/cart", { method: "DELETE", body: { product_id, size, color } }),
  checkout: (items = null) => http("/checkout", { method: "POST", body: { items } }),
  orders: () => http("/orders"),
  /* 주문 화면의 취소·반품 버튼. 실행이 아니라 확인 대기를 연다 (응답은 chat 과 같은 모양). */
  orderAction: (order_id, kind, reason) =>
    http(`/orders/${encodeURIComponent(order_id)}/action`, { method: "POST", body: { kind, reason } }),
  uploadImage: async (file) => {
    const res = await fetch(BASE + "/images", {
      method: "POST", headers: { "Content-Type": file.type }, body: file,
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.error || `이미지 업로드 실패 (${res.status})`);
    }
    return res.json();
  },
  chat: (message, query_image_id = null) =>
    http("/chat", { method: "POST", body: { message, query_image_id } }),
  approve: (keys) => http("/approve", { method: "POST", body: { keys } }),
  reject: () => http("/reject", { method: "POST" }),
  /* 확인 대기는 chat/approve 응답에 함께 실려 옵니다.
     여기서 별도 요청을 하면 동기 호출 자리에 Promise 가 들어가 화면이 깨집니다. */
  pending: () => http("/pending"),
  /* 새로고침 뒤 이어 그릴 상태 {messages, pending}. 시작할 때 한 번 부른다. */
  session: () => http("/session"),
  meta: () => http("/meta"),
  /* 찜 (v13). 응답의 wish_count = 추정 시작값 + 실제 찜. */
  wishlist: () => http("/wishlist"),
  setWish: (product_id, on) => http("/wishlist", { method: on ? "POST" : "DELETE", body: { product_id } }),
  /* 데모 사용자(info1~5) 전환. 로그인이 아니라 시연용 사용자 선택이다. */
  demoUsers: () => http("/demo-users"),
  switchUser: (user_id) => http("/switch-user", { method: "POST", body: { user_id } }),
  /* 지금 사용자만 처음 상태로 (장바구니·선호 비움, 주문은 데모 주문으로, 대화·승인 대기 삭제). */
  resetUser: () => http("/reset", { method: "POST" }),
};

export const api = USE_MOCK ? mock : real;
