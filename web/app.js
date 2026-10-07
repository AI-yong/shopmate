/* ==========================================================================
   SHOPMATE — 화면
   데이터는 전부 api.js 를 통해서만 가져옵니다 (지금은 목업).
   ========================================================================== */
import { api } from "./api.js";
import { art, swatchDot } from "./art.js";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const won = (n) => n.toLocaleString("ko-KR") + "원";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* 사이즈 표시 이름. 서버가 size_options 에 "ONE_SIZE → 단일 사이즈", "270 → 270mm"
   처럼 표시 이름을 준다. 장바구니·주문 줄은 서버가 size_label 을 붙여 보낸다.
   둘 다 없으면(mock, 옛 데이터) 코드를 그대로 쓴다. */
function sizeLabel(p, code) {
  const hit = (p?.size_options || []).find((o) => String(o.code) === String(code));
  return hit?.label || String(code);
}
/* 줄 설명의 "사이즈 270mm". 표시 이름이 이미 "단일 사이즈" 면 앞의 "사이즈" 를 뺀다. */
const sizeText = (label) => (String(label).endsWith("사이즈") ? String(label) : `사이즈 ${label}`);

// 상품 그림. 사진이 있으면 사진, 없으면 art.js 의 그림.
// 사진을 그림 **위에** 얹는다 — 사진이 안 뜨면(저장소 꺼짐, 404) img 가 스스로
// 사라져서 뒤의 그림이 보인다. 상태를 따로 들고 있을 필요가 없다.
function pic(p, { full = false } = {}) {
  const url = full ? (p.image_url || p.thumbnail_url) : (p.thumbnail_url || p.image_url);
  if (!url) return art(p);
  return `<div class="ph-wrap">${art(p)}<img class="ph" src="${esc(url)}" alt="${esc(p.name || "")}" loading="lazy" decoding="async" onerror="this.remove()"></div>`;
}

// 대분류 → 소분류. **서버가 준다** (/api/meta 의 category_groups). 여기 박아 두면
// 카탈로그가 바뀔 때마다 화면이 뒤처진다 — 아마존으로 갈아끼우면서 원피스·이너가
// 사라지고 가방·모자·벨트·스카프·기타가 생겼다. 아래 값은 서버가 없을 때(mock)만 쓴다.
let GROUPS = {
  "신발": ["운동화", "구두", "부츠", "샌들"],
  "상의": ["티셔츠", "셔츠", "니트", "후드"],
  "하의": ["팬츠", "스커트"],
  "아우터": ["재킷", "코트"],
};

/* 브랜드 이름 → 실제 표기. 헤더 검색어가 브랜드와 정확히 같으면 브랜드 칩으로 건다.
   정규화는 서버 store_pg._brand_key 와 같다: 대소문자·공백·구두점 무시, 추측 매칭 없음. */
const brandKey = (v) => String(v || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
let BRANDS = new Map();
let COLORS = [];                  // 색상 필터 목록 (/api/meta 의 colors)

async function loadGroups() {
  try {
    const m = await api.meta();
    if (m && m.category_groups && Object.keys(m.category_groups).length) {
      GROUPS = m.category_groups;
    }
    const byKey = new Map();
    for (const b of m?.brands || []) {
      const k = brandKey(b);
      if (k) byKey.set(k, byKey.has(k) ? null : b);   // 키가 겹치면 어느 쪽인지 모르므로 쓰지 않는다
    }
    BRANDS = byKey;
    COLORS = m?.colors || [];
  } catch (e) { /* 서버가 없으면 위 기본값으로 그린다 */ }
}

/* 헤더 검색어가 분류·브랜드 이름과 정확히 같은지. 같으면 검색어가 아니라 필터로 건다.
   서버(api_products)도 같은 판정을 하지만, 화면이 먼저 필터로 바꿔야 위쪽 카테고리
   메뉴와 칩이 실제로 걸린 조건과 일치한다 — 예전에는 "상의" 를 보다가 "운동화" 를 치면
   결과는 운동화인데 메뉴는 상의에 켜져 있었다. */
function typedFilter(text) {
  if (GROUPS[text]) return { group: text };
  const group = Object.entries(GROUPS).find(([, cats]) => cats.includes(text))?.[0];
  if (group) return { group, category: text };
  const brand = BRANDS.get(brandKey(text));
  if (brand) return { brand };
  return null;
}

const S = {
  view: "shop",
  query: "", group: null, category: null, gender: null, color: null,
  semanticQuery: null, searchArgs: null,
  // 상담의 추천·비교·사진 검색 결과. {label, rows}. 이 결과는 /api/products 로 다시
  // 만들 수 없다(사진 신호·추천 기준이 거기 없다). 그래서 이 값이 있는 동안은
  // 정렬·필터를 서버에 다시 묻지 않고 이 목록 안에서만 적용한다.
  chatPick: null,
  sort: "recommend",
  shown: 20, rows: [], totalRows: 0,
  detailId: null, detailSize: null,
  detailColor: null,          // 상세에서 고른 색. 담을 때 이 값이 서버로 간다
  detailProduct: null,        // 색을 바꿀 때 다시 받아오지 않으려고 들고 있는다
  picked: new Set(),          // 장바구니에서 고른 줄
  seenLines: new Set(),       // 장바구니 화면에 한 번 나타난 줄 (새 줄만 기본 선택하기 위해)
  wish: new Set(),
  messages: [{ role: "bot", text: "안녕하세요. 찾으시는 상품을 말로 알려주시면 제가 찾아 담아 드릴게요." }],
  pending: null, busy: false, scrollY: 0,
  imageQuery: null, imageUploading: false,
  // 장바구니 결제 확인 단계. {mode: "all"|"picked", total}. 버튼 한 번에 바로 결제되지
  // 않게, 금액을 보여 주고 한 번 더 누르게 한다 (상담창 결제의 승인 버튼과 같은 원칙).
  checkout: null,
};

/* 찜은 서버에 사용자별로 둔다 (/api/wishlist, v13). 예전에는 localStorage 에만 있어서
   카드의 하트 숫자가 실제 찜과 무관했다. 이제 상품마다 서버가 wish_count 를 준다
   (리뷰 수에서 추정한 시작값 + 실제 찜). */
async function loadWishlist() {
  try { S.wish = new Set((await api.wishlist()).product_ids || []); }
  catch { /* 못 읽으면 빈 찜으로 — 상품 목록은 그대로 보인다 */ }
}

/* 찜 수 표시. 줄여 쓰지 않는다 — "2.4천" 으로 줄이면 찜을 눌러 2,358 → 2,359 가 돼도 화면이 그대로다. */
const wishText = (n) => Number(n ?? 0).toLocaleString("ko-KR");

/* 찜을 누른 뒤 화면 곳곳의 같은 상품을 맞춘다 — 그리드 카드, 상담 결과 목록, 상세. */
function applyWish(id, count) {
  const touch = (p) => { if (p && p.id === id) { p.wish_count = count; p.wished = S.wish.has(id); } };
  S.rows.forEach(touch); (S.chatPick?.rows || []).forEach(touch); touch(S.detailProduct);
  $$(`[data-wish="${CSS.escape(id)}"]`).forEach((b) => b.classList.toggle("on", S.wish.has(id)));
  $$(`[data-likes="${CSS.escape(id)}"]`).forEach((el) => { el.lastChild.textContent = wishText(count); });
  $$(`[data-detail-wish="${CSS.escape(id)}"]`).forEach((b) => { b.textContent = detailWishLabel(id, count); });
}
const detailWishLabel = (id, count) => `${S.wish.has(id) ? "찜 해제" : "찜하기"} · ${wishText(count)}`;

/* ============================ 데모 사용자 ============================ */
/* 헤더의 "info1 ▾". 시연용 사용자 선택이지 로그인이 아니다 (비밀번호 없음).
   바꾸거나 처음부터 다시 하면 페이지를 새로 연다 — 대화·장바구니·승인 대기·찜이 전부
   사용자 것이라, 화면 상태를 하나씩 비우는 것보다 새로 읽는 편이 빠뜨릴 게 없다. */
let USER = null;                  // {users, current, is_demo}
const userLabel = () => (USER?.is_demo ? USER.current : "익명 사용자");

async function loadUserMenu() {
  if (typeof api.demoUsers !== "function") return;     // mock
  try { USER = await api.demoUsers(); } catch { return; }
  $("#userLabel").textContent = userLabel();
  $("#userMenu").hidden = false;
}

function renderUserPop() {
  $("#userPop").innerHTML = `
    <div class="up-h">데모 사용자 · 시연용 (로그인 아님)</div>
    ${USER.users.map((u) => `<button role="menuitem" data-switch-user="${esc(u)}"
        class="${u === USER.current ? "on" : ""}">${esc(u)}</button>`).join("")}
    <div class="up-sep"></div>
    <button role="menuitem" data-switch-user="">새 익명 사용자로 시작</button>
    <button role="menuitem" class="danger" data-reset-user="1">처음부터 다시</button>
    <small>${esc(userLabel())}의 장바구니·대화·찜을 비우고 주문을 처음 상태로 되돌립니다${USER?.is_demo ? "" : "(익명 사용자는 주문 없음)"}.</small>`;
}
function toggleUserPop(on) {
  if (on) renderUserPop();
  $("#userPop").hidden = !on;
  $("#userBtn").setAttribute("aria-expanded", String(on));
}
const restart = () => location.replace(location.pathname);   // 해시(보던 화면)도 버리고 처음 화면으로

/* ============================ 토스트 ============================ */
function toast(text, warn = false) {
  const el = document.createElement("div");
  el.className = "toast" + (warn ? " warn" : "");
  el.textContent = text;
  $("#toasts").append(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 1900);
  setTimeout(() => el.remove(), 2300);
}

/* ============================ 히어로 ============================ */
const SLIDES = [
  { kick: "TALK & SHOP", title: "말로 찾고, 말로 담고,<br>말로 취소하세요",
    sub: "상품 검색부터 주문 취소까지 — 필요한 도구는 에이전트가 고릅니다",
    cta: "상담 열기", act: "dock",
    bg: "linear-gradient(118deg,#101014 0%,#22222B 48%,#3E2224 100%)",
    dots: ["#E2231A", "#5B5BE0", "#E29A1A"] },
  { kick: "MOST REVIEWED", title: "리뷰가 가장 많이<br>쌓인 상품",
    sub: "평점과 리뷰가 함께 쌓인 상품부터 보여드립니다",
    cta: "인기 상품 보기", act: "popular",
    bg: "linear-gradient(118deg,#14212B 0%,#1E3442 52%,#2C4C4A 100%)",
    dots: ["#3FB0A0", "#7FD1C4", "#2C6BD1"] },
  { kick: "SAFE BY DESIGN", title: "되돌릴 수 없는 일은<br>버튼으로만",
    sub: "삭제 · 결제 · 취소 · 반품은 사람이 누르기 전에는 실행되지 않습니다",
    cta: "어떻게 동작하나", act: "dock",
    bg: "linear-gradient(118deg,#1B1520 0%,#2E2033 50%,#432433 100%)",
    dots: ["#E27AA8", "#B36BE0", "#E2231A"] },
];
let heroIx = 0, heroTimer = null;

function renderHero() {
  $("#heroTrack").innerHTML = SLIDES.map((s) => `
    <div class="slide" style="background:${s.bg}">
      <div class="slide-deco">
        ${s.dots.map((c, i) => `<i style="background:${c};width:${180 + i * 90}px;
          height:${180 + i * 90}px;right:${-40 + i * 120}px;top:${-60 + i * 40}px;
          opacity:${.34 - i * .08}"></i>`).join("")}
      </div>
      <div class="slide-copy">
        <div class="slide-kick" style="color:${s.dots[0]}">${s.kick}</div>
        <div class="slide-title">${s.title}</div>
        <div class="slide-sub">${s.sub}</div>
        <button class="slide-cta" data-hero-act="${s.act}">${s.cta} <span>&rarr;</span></button>
      </div>
      <div class="slide-art" data-slide-art></div>
    </div>`).join("");
  $("#heroDots").innerHTML = SLIDES.map((_, i) =>
    `<button data-hero-go="${i}" class="${i === 0 ? "on" : ""}"></button>`).join("");
  goHero(0);
  startHero();
}
function goHero(i) {
  heroIx = (i + SLIDES.length) % SLIDES.length;
  $("#heroTrack").style.transform = `translateX(-${heroIx * 100}%)`;
  $$("#heroDots button").forEach((b, k) => b.classList.toggle("on", k === heroIx));
}
function startHero() {
  clearInterval(heroTimer);
  heroTimer = setInterval(() => goHero(heroIx + 1), 5200);
}

/* ============================ 카테고리 네비 ============================ */
function renderCatnav() {
  const items = [["전체", null]].concat(Object.keys(GROUPS).map((g) => [g, g]));
  $("#catnav").innerHTML = items.map(([label, g]) =>
    `<button data-group="${esc(g ?? "")}" class="${S.group === g ? "on" : ""}">${esc(label)}</button>`
  ).join("");
}

/* ============================ 필터 칩 ============================ */
function renderChips() {
  const subs = S.group ? GROUPS[S.group] || [] : [];
  const chips = [];
  // 상담 결과를 보고 있다는 표시. × 를 누르면 일반 목록으로 돌아간다.
  if (S.chatPick) chips.push(
    `<button class="chip on pick-chip" data-clearpick="1">${esc(S.chatPick.label)} · ${S.chatPick.rows.length}개<span class="x">&times;</span></button>`);
  subs.forEach((c) => chips.push(
    `<button class="chip ${S.category === c ? "on" : ""}" data-cat="${esc(c)}">${esc(c)}</button>`));
  ["남성", "여성", "공용"].forEach((g) => chips.push(
    `<button class="chip ${S.gender === g ? "on" : ""}" data-gender="${g}">${g}</button>`));
  // 색은 분류·성별처럼 늘 고를 수 있는 필터다. 상담에서 색이 넘어오면 여기가 그 색으로 켜진다.
  if (COLORS.length || S.color) chips.push(`<div class="color-pick">
      <button class="chip ${S.color ? "on" : ""}" id="colorBtn" aria-haspopup="true">
        ${S.color ? `${swatchDot(S.color)}${esc(S.color)}` : "색상"}<span class="caret">▾</span></button>
      <div class="color-pop" id="colorPop" hidden></div></div>`);
  // 상담·검색에서 넘어온 나머지 조건(가격·사이즈·검색어·브랜드·소재·세탁·재고). 보이지 않게 두면
  // "왜 10만원 이하만 나오지?" 가 된다. "상담 조건" 이름표 뒤에 조건마다 칩 하나로 두고,
  //   - 이름 부분을 누르면 값을 고친다 (가격·사이즈·검색어)
  //   - × 는 그 조건 하나만 푼다
  //   - "모두 풀기" 는 전부 푼다 (분류·성별·색은 위에서 따로 보이므로 그대로 둔다)
  // 무엇을 바꾸든 나머지 조건은 그대로 두고 다시 검색한다.
  const conds = searchArgChips().map(([label, keys, kind]) => ({ label, keys, kind }));
  if (S.query) conds.push({ label: `"${S.query}"`, kind: "query", query: true });
  if (conds.length) {
    chips.push(`<span class="cond-label">${S.searchArgs ? "상담 조건" : "검색"}</span>`);
    conds.forEach((c, i) => chips.push(`<span class="chip on cond ${c.kind ? "editable" : ""}">
        ${c.kind ? `<button class="cond-edit" data-cond-edit="${c.kind}" data-cond-i="${i}" title="눌러서 고치기">${esc(c.label)}<span class="caret">▾</span></button>`
                 : `<span>${esc(c.label)}</span>`}
        <button class="x" ${c.query ? 'data-clear-query="1"' : `data-arg="${c.keys.join(",")}"`} aria-label="${esc(c.label)} 조건 풀기">&times;</button>
        ${c.kind ? `<div class="cond-pop" data-cond-pop="${i}" hidden></div>` : ""}
      </span>`));
    if (conds.length > 1) chips.push(`<button class="cond-all" data-clearq="1">모두 풀기</button>`);
  }
  $("#filterChips").innerHTML = chips.join("");
}

/* 사이즈 코드의 표시 이름을 지금 목록 상품들의 size_options 에서 찾는다 ("270" → "270mm"). */
function sizeLabelFor(code) {
  for (const p of S.rows) {
    const hit = (p.size_options || []).find((o) => String(o.code) === String(code));
    if (hit) return hit.label;
  }
  return String(code);
}

/* 조건 칩을 눌렀을 때 열리는 고치기 팝업. */
function condPopHTML(kind) {
  const a = S.searchArgs || {};
  if (kind === "price") return `
    <form class="cond-form" data-cond-form="price">
      <label>최소<input type="number" name="min" min="0" step="1000" placeholder="0" value="${a.min_price ?? ""}"></label>
      <span>~</span>
      <label>최대<input type="number" name="max" min="0" step="1000" placeholder="제한 없음" value="${a.max_price ?? ""}"></label>
      <button class="btn sm point" type="submit">적용</button>
    </form>`;
  if (kind === "query") return `
    <form class="cond-form" data-cond-form="query">
      <input type="text" name="q" value="${esc(S.query)}" placeholder="검색어">
      <button class="btn sm point" type="submit">적용</button>
    </form>`;
  if (kind === "size") {
    // 고를 수 있는 사이즈 = 지금 목록 상품들이 가진 사이즈 (같은 분류라 체계가 같다)
    const seen = new Map();
    S.rows.forEach((p) => (p.size_options || []).forEach((o) => seen.set(String(o.code), o.label)));
    if (!seen.size) Object.keys(S.rows[0]?.sizes || {}).forEach((c) => seen.set(c, c));
    return `<div class="cond-sizes">${[...seen].map(([code, label]) => `
      <button type="button" data-cond-size="${esc(code)}" class="${String(a.size) === code ? "on" : ""}">${esc(label)}</button>`).join("")}</div>`;
  }
  return "";
}
function closeCondPops() { $$(".cond-pop").forEach((el) => { el.hidden = true; }); }

/* 조건을 고친 뒤 다시 검색. 나머지 조건은 그대로다. */
function applyCond() {
  if (S.searchArgs && !Object.keys(S.searchArgs).length) S.searchArgs = null;
  S.shown = 20; renderChips(); loadGrid();
}

/* 색상 고르기 팝업. "전체" 는 색 조건을 푼다. 고르면 같은 검색 조건에 색만 바꿔 다시 불러온다. */
function renderColorPop() {
  $("#colorPop").innerHTML = `
    <button data-color-set="" class="${S.color ? "" : "on"}"><i class="swatch any"></i><span>전체</span></button>
    ${COLORS.map((c) => `<button data-color-set="${esc(c)}" class="${c === S.color ? "on" : ""}" title="${esc(c)}">
      ${swatchDot(c)}<span>${esc(c)}</span></button>`).join("")}`;
}
function toggleColorPop(on) {
  const pop = $("#colorPop"); if (!pop) return;
  if (on) renderColorPop();
  pop.hidden = !on;
}

/* S.searchArgs 중 전용 칩이 없는 조건을 [표시 문구, 지울 키들] 로 만든다.
   category·gender·color·group 은 위에서 이미 칩이 있으므로 여기서 제외합니다. */
function searchArgChips() {
  const a = S.searchArgs || {};
  const out = [];
  if (a.brand) out.push([a.brand, ["brand"]]);
  if (a.material) out.push([`소재 ${a.material}`, ["material"]]);
  // 세 번째 값은 칩을 눌러 고칠 수 있는 조건의 종류 (condPopHTML). 없으면 × 로 풀기만.
  if (a.min_price != null && a.max_price != null)
    out.push([`${won(a.min_price)} ~ ${won(a.max_price)}`, ["min_price", "max_price"], "price"]);
  else if (a.max_price != null) out.push([`${won(a.max_price)} 이하`, ["max_price"], "price"]);
  else if (a.min_price != null) out.push([`${won(a.min_price)} 이상`, ["min_price"], "price"]);
  if (a.size != null) out.push([`${sizeLabelFor(a.size)} 사이즈 재고`, ["size"], "size"]);
  if (a.machine_washable === true) out.push(["세탁기 사용 가능", ["machine_washable"]]);
  if (a.in_stock === true) out.push(["재고 있는 상품만", ["in_stock"]]);
  return out;
}

/* 둘러보기 상태를 처음으로. 로고를 누를 때 쓴다. */
function resetBrowse() {
  clearChatSearch();
  S.group = null; S.category = null; S.gender = null; S.color = null;
  S.sort = "recommend"; $("#sortSel").value = "recommend";
  S.shown = 20; S.scrollY = 0;       // 목록으로 돌아올 때 옛 스크롤 위치를 복원하지 않게
  goHero(0); startHero();
}

/* 상담 검색에서 넘어온 조건을 모두 지운다. 검색창·의미 검색어·부가 조건 전부. */
function clearChatSearch() {
  S.query = ""; S.semanticQuery = null; S.searchArgs = null; S.chatPick = null;
  $("#searchInput").value = ""; $("#searchClear").hidden = true;
}

/* ============================ 상품 그리드 ============================ */
function tileHTML(p, rank) {
  const sold = Object.values(p.sizes || {}).every((v) => v === 0);
  const wished = S.wish.has(p.id);
  const id = esc(p.id);
  return `<article class="tile" data-id="${id}" style="animation-delay:${Math.min(rank, 12) * 22}ms">
    <div class="tile-art" data-open="${id}">
      ${rank < 3 ? `<span class="tile-rank ${rank === 0 ? "top" : ""}">${rank + 1}</span>` : ""}
      ${pic(p)}
      ${sold ? '<div class="tile-soldout">품절</div>' : ""}
      <button class="tile-wish ${wished ? "on" : ""}" data-wish="${id}" aria-label="찜">
        <svg viewBox="0 0 24 24" class="ic"><path d="M12 20s-7-4.4-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.6-7 9-7 9z"/></svg>
      </button>
      ${sold ? "" : `<div class="tile-quick">
        <button class="btn sm point" data-quick="${id}">바로 담기</button>
      </div>`}
    </div>
    <div class="tile-info">
      <div class="tile-brand">${esc(p.brand)}</div>
      <div class="tile-name">${esc(p.name)}</div>
      <div class="tile-price">${p.price.toLocaleString("ko-KR")}<small>원</small></div>
      <div class="tile-meta">
        <span class="stars">${(p.rating ?? 0).toFixed(1)}</span><span>(${(p.review_count ?? 0).toLocaleString()})</span>
        ${colorsBrief(p)}
        <span class="tile-likes" data-likes="${id}" title="찜 ${(p.wish_count ?? 0).toLocaleString()}"><svg viewBox="0 0 24 24" class="ic"><path d="M12 20s-7-4.4-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.6-7 9-7 9z"/></svg>${wishText(p.wish_count)}</span>
      </div>
      ${tagsHTML(p)}
    </div>
  </article>`;
}

/* 카드의 색 표시. 색이 여럿이면 점을 최대 4개까지 찍고 나머지는 숫자로 줄입니다.
   이름을 다 쓰면 카드 한 줄이 넘칩니다. */
function colorsBrief(p) {
  const cs = p.colors || [];
  if (!cs.length) return "";
  if (cs.length === 1) return `${swatchDot(cs[0].color)}${esc(cs[0].color)}`;
  const dots = cs.slice(0, 4).map((c) => swatchDot(c.color)).join("");
  return `<span class="tile-colors">${dots}${cs.length > 4 ? `+${cs.length - 4}` : ""}
    <span class="tile-colors-n">${cs.length}색</span></span>`;
}

/* 카드 배지. 전부 데이터에서 나오는 값이라 지어낸 게 없습니다.
   BEST 는 리뷰 400개 이상, 빠른배송은 배송 1일, 세탁기는 machine_washable. */
function tagsHTML(p) {
  const tags = [];
  if (p.review_count >= 400) tags.push('<span class="tag-s best">BEST</span>');
  if (p.delivery_days != null && p.delivery_days <= 1) tags.push('<span class="tag-s fast">빠른배송</span>');
  return tags.length ? `<div class="tile-tags">${tags.join("")}</div>` : "";
}

function skeletons(n = 12) {
  return Array.from({ length: n }, () =>
    `<div><div class="sk sk-art"></div><div class="sk sk-l" style="width:38%"></div>
     <div class="sk sk-l" style="width:76%"></div><div class="sk sk-l" style="width:46%"></div></div>`
  ).join("");
}

/* 상담 결과(S.chatPick) 안에서만 필터·정렬한다. "추천순" 은 Tool 이 준 순서 그대로다. */
const SORTS = {
  wish: (a, b) => (b.wish_count ?? 0) - (a.wish_count ?? 0),
  price_asc: (a, b) => a.price - b.price,
  price_desc: (a, b) => b.price - a.price,
  rating: (a, b) => (b.rating ?? 0) - (a.rating ?? 0),
  review: (a, b) => (b.review_count ?? 0) - (a.review_count ?? 0),
};
function chatPickRows() {
  const inGroup = (p) => !S.group || (GROUPS[S.group] || []).includes(p.category) || p.group === S.group;
  const rows = S.chatPick.rows.filter((p) =>
    inGroup(p)
    && (!S.category || p.category === S.category)
    && (!S.gender || p.gender === S.gender)
    && (!S.color || (p.colors || []).some((c) => c.color === S.color)));
  const cmp = SORTS[S.sort];
  return cmp ? rows.slice().sort(cmp) : rows;
}

/* 그리드 요청 번호. 의미 검색은 10초 가까이 걸리고 SQL 필터는 0.1초라, 먼저 보낸
   느린 요청이 나중에 도착해 새 조건의 결과를 덮어쓸 수 있다. 요청마다 번호를 받고,
   응답이 왔을 때 번호가 최신이 아니면 버린다. 상담 결과로 그리드를 바꿀 때도 번호를
   올려서 그 전에 나간 요청이 상담 결과를 덮지 못하게 한다. */
let gridSeq = 0;
const nextGridSeq = () => ++gridSeq;

function gridError(message) {
  $("#grid").innerHTML = `<div class="empty" style="grid-column:1/-1"><b>상품을 불러오지 못했습니다</b>
    ${esc(message)}<br><button class="btn ghost sm" data-retry-grid="1" style="margin-top:14px">다시 시도</button></div>`;
  $("#resultCount").textContent = "";
  $("#moreBtn").hidden = true;
}

async function loadGrid(append = false) {
  const seq = nextGridSeq();
  if (S.chatPick) {
    S.rows = chatPickRows();
    S.totalRows = S.rows.length;
    renderGrid();
    return;
  }
  if (!append) $("#grid").innerHTML = skeletons();
  let page;
  try {
    page = await fetchGridPage(append);
  } catch (err) {
    if (seq !== gridSeq) return;
    if (append) toast(err.message || "더 불러오지 못했습니다", true);
    else gridError(err.message || "");
    return;
  }
  if (seq !== gridSeq) return;       // 그사이 조건이 바뀌었다 — 이 응답은 낡았다
  if (append) {
    const seen = new Set(S.rows.map((row) => row.id));
    S.rows.push(...page.filter((row) => !seen.has(row.id)));
  } else {
    S.rows = page;
  }
  S.totalRows = page.totalCount ?? S.rows.length;
  renderGrid();
}

function fetchGridPage(append) {
  return api.products({
    query: S.query, group: S.group, category: S.category,
    gender: S.gender, color: S.color, sort: S.sort,
    semanticQuery: S.semanticQuery,
    productName: S.searchArgs?.product_name,
    brand: S.searchArgs?.brand, material: S.searchArgs?.material,
    minPrice: S.searchArgs?.min_price, maxPrice: S.searchArgs?.max_price,
    size: S.searchArgs?.size,
    machineWashable: S.searchArgs?.machine_washable === true ? 1 : null,
    inStock: S.searchArgs?.in_stock === true ? 1 : null,
    offset: append ? S.rows.length : 0,
    limit: 60,
  });
}
/* 히어로 오른쪽 상품 콜라주. 실제 목록에서 고른 상품이라 클릭하면 상세로 갑니다.
   슬라이드마다 다른 셋을 보여 줍니다. */
function renderHeroArt() {
  const pool = S.rows.filter((p) => !Object.values(p.sizes || {}).every((v) => v === 0));
  if (pool.length < 3) return;
  $$("[data-slide-art]").forEach((el, k) => {
    if (el.children.length) return;                 // 한 번 채웠으면 그대로 둔다
    const picks = [0, 1, 2].map((i) => pool[(k * 3 + i) % pool.length]);
    el.innerHTML = picks.map((p) => `<div class="pc" data-open="${esc(p.id)}">${pic(p)}</div>`).join("");
  });
}

function renderGrid() {
  renderHeroArt();
  const shown = S.rows.slice(0, S.shown);
  $("#grid").innerHTML = shown.length
    ? shown.map(tileHTML).join("")
    : `<div class="empty" style="grid-column:1/-1"><b>조건에 맞는 상품이 없습니다</b>
       검색어를 지우거나 필터를 넓혀 보세요</div>`;
  $("#resultCount").textContent =
    `${S.totalRows.toLocaleString()}개 중 ${shown.length}개`;
  $("#moreBtn").hidden = shown.length >= S.totalRows;
  $("#moreBtn").textContent = `더 보기 (${(S.totalRows - shown.length).toLocaleString()}개 남음)`;
}

/* ============================ 상세 ============================ */
/* 화면 하나를 그리다 실패했을 때. 이유를 보여 주고 돌아갈 길을 둔다. */
function viewError(view, err) {
  const title = view === "detail" && err?.status === 404 ? "상품을 찾을 수 없습니다"
    : { detail: "상품을 불러오지 못했습니다", cart: "장바구니를 불러오지 못했습니다",
        orders: "주문 내역을 불러오지 못했습니다" }[view] || "화면을 불러오지 못했습니다";
  const detail = err?.status === 404 ? "판매가 끝났거나 주소가 잘못되었습니다" : (err?.message || "");
  $("#view-" + view).innerHTML = `<div class="empty"><b>${esc(title)}</b>${esc(detail)}<br>
    <button class="btn ghost sm" data-nav="shop" style="margin-top:14px">상품 목록으로</button></div>`;
}

async function renderDetail(id) {
  const p = await api.product(id);
  // 상품을 빠르게 연달아 열면 먼저 연 상품의 응답이 늦게 와서 화면을 덮을 수 있다.
  const now = fromHash();
  if (now.view !== "detail" || now.arg !== id) return;
  S.detailId = id;
  S.detailProduct = p;
  // 재고가 있는 색을 기본으로 고릅니다. 전부 품절이면 첫 색을 보여주고,
  // 사이즈 버튼이 전부 "품절" 로 나옵니다.
  const colors = p.colors || [];
  S.detailColor = (colors.find((c) => c.in_stock) || colors[0] || {}).color ?? null;
  S.detailSize = firstSizeInStock(p, S.detailColor);
  // 값은 전부 여기서 이스케이프한다. 아마존 공개 메타데이터라 소재·브랜드에 & < 가 섞여 온다.
  // 색상 줄만 swatchDot(HTML) 이 들어가므로 이름을 따로 이스케이프해서 조립한다.
  const rows = [
    ["브랜드", esc(p.brand)], ["분류", esc(`${p.group} · ${p.category}`)],
    ["색상", colors.map((c) => `${swatchDot(c.color)} ${esc(c.color)}`).join(" · ") || "-"],
    ["소재", esc(p.material_detail || p.material || "-")],
    ["성별", esc(p.gender)],
    // 세탁 안내는 소재·분류에서 규칙으로 추정한 값이다(care_rules.py). 아마존 원본에
    // 세탁 표기가 없어서, 예전처럼 "세탁기 사용 불가 · 드라이 권장" 으로 일괄 표시하면 틀린다.
    ["세탁", esc(p.care || (p.machine_washable ? "세탁기 사용 가능" : "세탁 표기를 확인하세요"))],
    ["배송", p.delivery_days != null ? `주문 후 약 ${esc(p.delivery_days)}일` : "-"],
  ];
  $("#view-detail").innerHTML = `
    <div class="crumb"><button data-nav="shop">상품</button><span>›</span>
      <span>${esc(p.group)}</span><span>›</span><span>${esc(p.category)}</span></div>
    <div class="dt">
      <div class="dt-art" id="dtArt">${pic({ ...p, color: S.detailColor }, { full: true })}</div>
      <div>
        <div class="dt-brand">${esc(p.brand)}</div>
        <h1 class="dt-name">${esc(p.name)}</h1>
        <div class="tile-meta" style="margin-top:10px">
          <span class="stars">★ ${(p.rating ?? 0).toFixed(1)}</span><span>리뷰 ${(p.review_count ?? 0).toLocaleString()}개</span></div>
        <div class="dt-price">${won(p.price)}</div>
        <p class="dt-desc">${esc(p.description)}</p>
        <div id="pickWrap">${pickerHTML(p)}</div>
        <div class="dt-actions">
          <button class="btn ghost" data-detail-wish="${esc(p.id)}">${detailWishLabel(p.id, p.wish_count)}</button>
          <button class="btn point" data-detail-add="${esc(p.id)}">장바구니에 담기</button>
        </div>
        <div class="dt-rows">
          ${rows.map(([k, v]) => `<div class="dt-row"><span class="k">${k}</span>
            <span class="v">${v}</span></div>`).join("")}
        </div>
      </div>
    </div>`;
}

/* 장바구니 한 줄을 가리키는 문자열. 색에 "|" 가 들어갈 일은 없습니다
   (팔레트가 열두 색 고정). data-* 속성으로 오가야 해서 문자열로 씁니다. */
function lineKey(l) {
  return [l.product_id, l.color || "", l.size].join("|");
}
function parseLineKey(k) {
  const [id, color, size] = k.split("|");
  return { id, color: color || null, size };
}

/* 상세의 색·사이즈 고르는 부분.
   색을 바꾸면 사이즈 재고가 통째로 바뀌므로(검은색 270 은 있는데 네이비 270 은
   없다) 둘을 한 덩이로 그리고, 색을 누를 때 이 덩이만 다시 그립니다. */
function pickerHTML(p) {
  const colors = p.colors || [];
  const sizes = sizesOf(p, S.detailColor);
  const colorRow = colors.length <= 1 ? "" : `
    <div style="margin-top:22px" class="lbl">색상</div>
    <div class="clr-grid" id="clrGrid">
      ${colors.map((c) => `
        <button class="clr ${c.in_stock ? "" : "out"} ${c.color === S.detailColor ? "on" : ""}"
                data-color-pick="${esc(c.color)}" title="${esc(c.color)}">
          ${swatchDot(c.color)}<span>${esc(c.color)}</span>
          ${c.in_stock ? "" : "<i>품절</i>"}
        </button>`).join("")}
    </div>`;
  return `${colorRow}
    <div style="margin-top:22px" class="lbl">사이즈</div>
    <div class="sz-grid" id="szGrid">
      ${Object.entries(sizes).map(([sz, q]) => `
        <button class="sz ${q === 0 ? "out" : ""} ${String(sz) === String(S.detailSize) ? "on" : ""}"
                data-size="${esc(sz)}"><b>${esc(sizeLabel(p, sz))}</b><i>${q === 0 ? "품절" : q + "개"}</i></button>`).join("")}
    </div>`;
}

/* 고른 색의 사이즈별 재고. 색을 못 고른 경우(색이 없는 옛 데이터)만 합계로 물러섭니다. */
function sizesOf(p, color) {
  const hit = (p.colors || []).find((c) => c.color === color);
  return hit ? hit.sizes : p.sizes;
}
function firstSizeInStock(p, color) {
  return Object.entries(sizesOf(p, color)).find(([, q]) => q > 0)?.[0] ?? null;
}

/* ============================ 장바구니 ============================ */
async function renderCart() {
  const v = await api.cart();
  $("#cartCount").textContent = v.quantity;   // 다시 받은 김에 헤더 숫자도 맞춘다
  if (!v.lines.length) {
    S.picked.clear(); S.seenLines.clear();
    $("#view-cart").innerHTML = `<div class="page-hd"><div><h2>장바구니</h2></div></div>
      <div class="empty"><b>장바구니가 비어 있습니다</b>상품을 담으면 여기에서 한 번에 주문할 수 있습니다</div>`;
    return;
  }
  // 줄의 열쇠 = (상품, 색, 사이즈). 색이 variant 가 된 뒤로 상품+사이즈만으로는
  // 검은색 270 과 네이비 270 이 같은 줄로 뭉쳐 보입니다.
  const key = (l) => lineKey(l);
  // 새로 담긴 줄만 기본 선택합니다. 예전에는 매 렌더마다 모든 줄을 picked 에
  // 다시 넣어서, 체크를 풀어도 다시 그리는 순간 다시 체크되는 버그가 있었습니다
  // ("전체 해제" 도 같은 이유로 동작하지 않았습니다). 한 번 본 줄은 seenLines 에
  // 기억해 두고, 이후 선택 상태는 사용자의 클릭만 바꿉니다.
  const present = new Set(v.lines.map(key));
  [...S.seenLines].forEach((k) => {           // 사라진 줄(상담으로 뺀 것 등)은 잊는다
    if (!present.has(k)) { S.seenLines.delete(k); S.picked.delete(k); }
  });
  v.lines.forEach((l) => {
    const k = key(l);
    if (!S.seenLines.has(k)) { S.seenLines.add(k); S.picked.add(k); }
  });
  const pickedLines = v.lines.filter((l) => S.picked.has(key(l)));
  const total = pickedLines.reduce((a, l) => a + l.price * l.quantity, 0);
  $("#view-cart").innerHTML = `
    <div class="page-hd">
      <div><h2>장바구니</h2><p>${v.lines.length}종 · ${v.quantity}개</p></div>
      <button class="btn ghost sm" id="pickAll">전체 선택 / 해제</button>
    </div>
    ${v.lines.map((l) => `
      <div class="line-row">
        <button class="pick ${S.picked.has(key(l)) ? "on" : ""}" data-pick="${esc(key(l))}">
          <svg viewBox="0 0 24 24" class="ic" style="width:13px;height:13px"><path d="M5 12l5 5 9-10"/></svg>
        </button>
        <div class="line-art" data-open="${esc(l.product_id)}">${pic(l)}</div>
        <div class="line-mid">
          <div class="tile-brand">${esc(l.brand)}</div>
          <div class="line-name">${esc(l.name)}</div>
          <div class="line-sub">${esc(sizeText(l.size_label || l.size))}${l.color ? ` · ${swatchDot(l.color)} ${esc(l.color)}` : ""}</div>
        </div>
        <div class="line-right">
          <div class="line-price">${won(l.price * l.quantity)}</div>
          <div class="qty">
            <button data-q="-1" data-k="${esc(key(l))}">−</button>
            <span>${l.quantity}</span>
            <button data-q="1" data-k="${esc(key(l))}">+</button>
          </div>
          <button class="btn ghost sm" data-del="${esc(key(l))}">빼기</button>
        </div>
      </div>`).join("")}
    ${S.checkout ? checkoutConfirmHTML(v, pickedLines) : `
    <div class="sum">
      <div class="sum-total">선택한 ${pickedLines.length}종 합계<b>${won(total)}</b></div>
      <button class="btn ghost" id="buyAll">전체 주문</button>
      <button class="btn point" id="buyPicked" ${pickedLines.length ? "" : "disabled"}>
        선택 항목만 주문</button>
    </div>`}`;
}

/* 결제 대상 줄. "전체" 는 장바구니 전부, "선택" 은 체크한 줄. */
function checkoutLines(v, mode) {
  return mode === "all" ? v.lines : v.lines.filter((l) => S.picked.has(lineKey(l)));
}
const linesTotal = (lines) => lines.reduce((a, l) => a + l.price * l.quantity, 0);

function checkoutConfirmHTML(v) {
  const lines = checkoutLines(v, S.checkout.mode);
  S.checkout.total = linesTotal(lines);
  const qty = lines.reduce((a, l) => a + l.quantity, 0);
  return `
    <div class="sum sum-confirm">
      <div class="sum-total">${S.checkout.mode === "all" ? "전체" : "선택한"} ${lines.length}종 ${qty}개를
        결제할까요?<b>${won(S.checkout.total)}</b></div>
      <button class="btn ghost" id="checkoutCancel">취소</button>
      <button class="btn point" id="checkoutGo">${won(S.checkout.total)} 결제하기</button>
    </div>`;
}

/* ============================ 주문 ============================ */
const BADGE = { "배송 준비 중": "prep", "배송 중": "ship", "배송 완료": "done",
                "취소됨": "cancel", "반품 신청됨": "ret" };
async function renderOrders() {
  const rows = await api.orders();
  $("#ordersCount").textContent = rows.length;
  $("#view-orders").innerHTML = `
    <div class="page-hd"><div><h2>주문 내역</h2><p>${rows.length}건</p></div></div>
    ${rows.map((o) => `
      <div class="line-row">
        <div class="line-art" data-open="${esc(o.items[0].product_id)}">${pic(o.items[0])}</div>
        <div class="line-mid">
          <div class="line-sub" style="margin:0 0 6px">${esc(o.id)} · ${o.ordered_days_ago}일 전</div>
          <div class="line-name">${esc(o.items[0].name)}
            ${o.items.length > 1 ? ` 외 ${o.items.length - 1}건` : ""}</div>
          <div class="line-sub">${esc(sizeText(o.items[0].size_label || o.items[0].size))}${o.items[0].color ? ` · ${esc(o.items[0].color)}` : ""} · ${o.items[0].quantity}개</div>
        </div>
        <div class="line-right">
          <span class="badge ${BADGE[o.status] || ""}">${esc(o.status)}</span>
          <div class="line-price">${won(o.total)}</div>
          ${orderActionsHTML(o)}
        </div>
      </div>`).join("")}`;
}

/* 주문 카드의 취소·반품 버튼.
   판정은 화면이 하지 않는다. 서버가 store.can_cancel / can_return 로 계산해 준
   actions 를 그대로 그린다 — 에이전트가 Tool 로 묻는 것과 같은 함수다.
   버튼을 눌러도 바로 실행되지 않고 상담창에 승인 버튼이 뜬다(채팅으로 부탁했을 때와
   같은 경로). 둘 다 안 되면 이유와 대안을 그대로 보여 준다. */
function orderActionsHTML(o) {
  const a = o.actions || {};
  if (a.cancel?.allowed)
    return `<button class="btn ghost sm" data-order-act="cancel" data-order-id="${esc(o.id)}" title="${esc(a.cancel.reason || "")}">주문 취소</button>`;
  if (a.return?.allowed)
    return `<button class="btn ghost sm" data-order-act="return" data-order-id="${esc(o.id)}" title="${esc(a.return.reason || "")}">반품 신청</button>`;
  const reason = a.cancel?.reason || a.return?.reason || "";
  const alt = a.cancel?.alternative || a.return?.alternative || "";
  if (!reason && !alt) return "";
  return `<div class="line-note">${esc(reason)}${alt ? `<b>${esc(alt)}</b>` : ""}</div>`;
}

/* ============================ 상담 도크 ============================ */
const SAMPLES = ["15만원 이하 검은색 운동화 보여줘", "여성 니트 평점 높은 거",
                 "장바구니에 뭐 담겨 있어?", "어제 주문한 거 취소해줘"];

function renderSamples() {
  $("#dockSamples").innerHTML = SAMPLES.map((s) =>
    `<button data-sample="${esc(s)}"><span>${esc(s)}</span><svg viewBox="0 0 24 24" class="ic"><path d="M5 12h14M13 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></button>`).join("");
}

/* 상담원 표식. 이모지 대신 SVG 모노그램을 쓴다. */
const BOT_MARK = `<div class="av" aria-hidden="true"><svg viewBox="0 0 32 32"><path d="M10 12V9.5a6 6 0 0 1 12 0V12h3.2l1.3 14.5H5.5L6.8 12H10zm2.2 0h7.6V9.5a3.8 3.8 0 0 0-7.6 0V12z"/></svg></div>`;

/* 모델 답변 본문을 다듬어 그린다.
   - **굵게** 만 허용 (모델이 마크다운을 섞어 쓴다)
   - "─ 실행 결과 ─" 아래 ✓/✗ 줄은 앱이 붙인 사실이므로 별도 블록으로 뗀다 */
function richText(text, products = [], msgIndex = null) {
  const [body, report] = String(text).split(/\n*─ 실행 결과 ─\n?/);
  const bold = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  // 문단 안에서 "1. …" 로 시작하는 줄이 이어지는 구간은 상품 목록으로, 나머지 줄은 보통 문단으로.
  // 예전에는 문단의 모든 줄이 번호일 때만 목록으로 봐서, 앱이 목록 앞에 "우선 볼 만한 상품이에요."
  // 같은 안내 줄을 붙이자(agent._search_grid_reply) 목록 전체가 클릭 안 되는 글자로 돌아갔다.
  const isPick = (l) => /^\d+\.\s/.test(l);
  let html = body.trim().split(/\n{2,}/).map((para) => {
    const lines = para.split("\n").map((l) => l.trim()).filter(Boolean);
    const out = [];
    for (let i = 0; i < lines.length;) {
      const run = [];
      while (i < lines.length && isPick(lines[i])) run.push(lines[i++]);
      if (run.length) { out.push(pickListHTML(run, products, bold, msgIndex)); continue; }
      const text = [];
      while (i < lines.length && !isPick(lines[i])) text.push(lines[i++]);
      out.push(`<p>${text.map(bold).join("<br>")}</p>`);
    }
    return out.join("");
  }).join("");
  if (report) {
    const rows = report.split("\n").map((l) => l.trim()).filter(Boolean);
    html += `<ul class="report">${rows.map((l) => {
      const ok = l.startsWith("✓"), bad = l.startsWith("✗");
      const t = (ok || bad) ? l.slice(1).trim() : l;
      return `<li class="${ok ? "ok" : bad ? "bad" : "note"}"><i></i><span>${bold(t)}</span></li>`;
    }).join("")}</ul>`;
  }
  return html;
}

/* 검색 턴의 상품 목록 ("1. 상품명 · 브랜드 · 가격 · 색 · 사이즈").
   앱(agent._search_grid_reply)이 쓰는 줄이라 모양이 일정하다. 번호는 그리드 순서와 같고
   상품명은 카탈로그에서 유일하다(중복 이름 정리 6d11e49). 줄의 상품명을 이 답과 함께 온
   검색 결과(search_results)에서 찾아 상세로 가는 버튼으로 만든다. 못 찾으면(새로고침으로
   복원한 대화 등) 글자만 보기 좋게 그린다 — 링크를 추측해서 달지 않는다. */
function pickListHTML(lines, products, bold, msgIndex = null) {
  const byName = new Map((products || []).map((p) => [p.name, p]));
  // 채팅에는 상위 몇 개만 적는다(agent.CHAT_PICKS). 전체는 그리드에 있으므로 그리로 가는 길을 둔다 —
  // "검색 결과 5개" 라고 해 놓고 3개만 보이면 나머지가 어디 있는지 알 수 없었다.
  const more = (products || []).length > lines.length && msgIndex != null
    ? `<li class="picks-more"><button data-show-grid="${msgIndex}">검색 결과 전체 ${products.length}개 보기
        <svg viewBox="0 0 24 24" class="ic"><path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button></li>`
    : "";
  return `<ol class="picks">${lines.map((line) => {
    const m = line.match(/^(\d+)\.\s+(.*)$/);
    const [name, ...meta] = m[2].split(" · ");
    const hit = byName.get(name.trim()) || (products || [])[Number(m[1]) - 1];
    const ok = hit && hit.name === name.trim();
    const inner = `<i>${m[1]}</i><span><b>${bold(name)}</b>${meta.length ? `<em>${esc(meta.join(" · "))}</em>` : ""}</span>`;
    return ok
      ? `<li><button data-open="${esc(hit.id)}" title="상품 보기">${inner}<svg viewBox="0 0 24 24" class="ic"><path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button></li>`
      : `<li><div>${inner}</div></li>`;
  }).join("")}${more}</ol>`;
}

/* 사진 검색이 무엇을 보고, 어떤 경로로, 어떤 조건으로 찾았는지.
   경로가 둘이라 서버(trace_json)가 주는 모양도 둘이다.
   - Qwen3-VL 통합 경로: 사진(+영어 검색문)을 한 벡터로. 사진 추정값은 조건으로 쓰지 않는다.
   - SigLIP+KURE RRF: 사진 설명·사용자 의미 검색어를 갈래별로 찾고 순위만 합친다.
     Qwen 이 실패해 내려온 경우(fallback)도 여기다.
   어느 경로였는지가 화면에 드러나야 결과를 해석할 수 있다. */
const ROUTE_LABELS = {
  qwen_fused: "Qwen3-VL · 사진+글", qwen_image: "Qwen3-VL · 사진만",
  gme_fused: "GME · 사진+글", gme_image: "GME · 사진만",
  rrf_siglip_kure: "SigLIP+KURE", siglip_filtered: "SigLIP", siglip_only_fallback: "SigLIP",
};
function planHTML(plan) {
  if (!plan) return "";
  const rows = [];
  const kv = (o) => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join(", ");
  if (plan.route) {
    const label = ROUTE_LABELS[plan.route] || plan.route;
    rows.push(["검색 경로", plan.fallback ? `${label} (통합 검색 실패 → 대체 경로)` : label]);
  }
  if (plan.visual_analysis_available === false) rows.push(["사진 이해", "사용 불가 (VLM 실패 → SigLIP+텍스트로 계속)"]);
  else if (plan.visual_summary) rows.push(["사진 이해", plan.visual_summary + (plan.analysis_cached ? " (캐시)" : "")]);
  if (plan.retrieval_query_en) rows.push(["영어 검색문", plan.retrieval_query_en]);
  if (plan.visual_retrieval_query) rows.push(["사진 설명 → KURE", plan.visual_retrieval_query]);
  if (plan.semantic_retrieval_query) rows.push(["사용자 의미 → KURE", plan.semantic_retrieval_query]);
  const ref = kv(plan.reference_filters);
  if (ref) rows.push(["기준 필터(완화 가능)", ref + ((plan.reference_filters_relaxed || []).length ? ` · 풀림: ${plan.reference_filters_relaxed.join(", ")}` : "")]);
  const user = kv(plan.user_filters);
  if (user) rows.push(["사용자 필터(강제)", user]);
  const soft = kv(plan.unapplied_soft_filters);
  if (soft) rows.push(["사진 추정(조건 미사용)", soft]);
  const negated = kv(plan.negated_filters);
  if (negated) rows.push(["제외한 조건", negated]);
  if (plan.relative_applied === false && plan.relative_preferences) rows.push(["상대 조건", "기록만, 순위 미반영"]);
  if (!rows.length) return "";
  return `<dl class="trace-plan">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

/* 실행 기록 한 단계의 종류. Tool 이 success:false 를 돌려도 전부 오류는 아니다 (server.trace_json 의 status).
     ok    성공
     ask   사용자에게 물어야 함 (성별·사진 속 품목·사이즈·색 …) — 정상적인 되묻기
     none  조건에 맞는 것 없음 · 규칙상 불가 (배송 시작 뒤 취소, 품절 …) — 결과일 뿐 오류가 아니다
     bad   인자 오류·처리 오류 — 진짜 실패 (모델이 고쳐서 다시 부르기도 한다)
   예전에는 success 만 보고 셌기 때문에, 성별을 묻는 단계까지 "실패 1" 로 보였다. */
function traceKind(t) {
  if (t.ok) return "ok";
  if (t.status === "needs_input") return "ask";
  if (t.status === "no_match" || t.status === "blocked") return "none";
  return "bad";
}
const TRACE_TAG = { ask: "질문", none: "결과 없음·불가", bad: "실패" };

function msgHTML(m, i) {
  if (m.role === "me")
    return `<div class="msg me"><div class="bubble">${esc(m.text)}</div></div>`;
  const steps = (m.trace || []).map((t) => ({ ...t, kind: traceKind(t) }));
  const tally = (k) => steps.filter((t) => t.kind === k).length;
  const summary = [`${steps.length}단계`,
    tally("ask") && `질문 ${tally("ask")}`,
    tally("none") && `결과 없음·불가 ${tally("none")}`,
    tally("bad") && `실패 ${tally("bad")}`].filter(Boolean).join(" · ");
  const trace = steps.length ? `
    <details class="trace"><summary><span class="trace-k">실행 기록</span><span class="trace-c ${tally("bad") ? "has-bad" : ""}">${summary}</span><svg viewBox="0 0 24 24" class="ic chev"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></summary>
      <ol class="trace-in">${steps.map((t) => `
        <li class="trace-row ${t.kind === "ok" ? "" : t.kind}"><i></i>
          <div><code class="trace-call"><b>${esc(t.name)}</b>${t.kind === "ok" ? "" : `<em class="trace-tag">${TRACE_TAG[t.kind]}</em>`}<span>${esc(JSON.stringify(t.args))}</span></code>
          ${t.msg ? `<div class="trace-msg">${esc(t.msg)}</div>` : ""}${planHTML(t.plan)}</div></li>`).join("")}
      </ol></details>` : "";
  // 상품은 채팅 안에 카드로 그리지 않는다. 항상 메인 그리드 한 곳에서 본다(2026-09-22 결정).
  return `<div class="msg bot">${BOT_MARK}
    <div class="msg-col"><div class="bubble">${richText(m.text, m.products, i)}</div>${trace}</div></div>`;
}

/* 첫 화면 — 아직 대화가 없을 때 무엇을 시킬 수 있는지 보여 준다. */
function welcomeHTML() {
  const rows = [
    ["M4 7h16M4 12h10M4 17h7", "상품 찾기·비교", "조건을 말하면 실제 재고에서 찾아 비교해 드려요"],
    ["M5 6h14l-1.5 10h-11z M9 20h.01M15 20h.01", "장바구니·결제", "담기와 결제까지, 결제는 버튼으로 한 번 더 확인"],
    ["M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z", "주문 조회·취소·반품", "어제 주문한 것, 지난주 받은 것처럼 말해도 찾습니다"],
  ];
  return `<div class="welcome">${rows.map(([d, t, sub]) => `
    <div class="wl"><svg viewBox="0 0 24 24"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <div><b>${t}</b><span>${sub}</span></div></div>`).join("")}</div>`;
}

function renderDock() {
  $("#dockBody").innerHTML = S.messages.map(msgHTML).join("") +
    (S.messages.length <= 1 && !S.busy ? welcomeHTML() : "") +
    (S.busy ? `<div class="msg bot">${BOT_MARK}
       <div class="bubble typing"><i></i><i></i><i></i><span>찾고 있어요</span></div></div>` : "");
  $("#dockBody").scrollTop = $("#dockBody").scrollHeight;
  $("#dockState").textContent = S.busy ? "생각하는 중" : "대기 중";

  const p = S.pending;
  $("#dockPending").hidden = !p;
  armPendingExpiry(p);
  if (p) {
    const mins = p.expires_in != null ? Math.max(1, Math.round(p.expires_in / 60)) : null;
    $("#dockPending").innerHTML = `
      <h4>확인이 필요합니다</h4>
      <p>되돌릴 수 없는 작업이라 버튼으로만 진행됩니다. 채팅으로 "네"라고 답해도 실행되지 않습니다.${
        mins ? ` 약 ${mins}분 안에 눌러 주세요.` : ""}</p>
      ${p.items.map((it, i) => `<div class="pend-item"><i>${i + 1}</i>${esc(it.label)}</div>`).join("")}
      <div class="pend-btns">
        <button class="btn point" data-approve="all">${p.items.length > 1 ? "모두 승인" : "승인"}</button>
        ${p.items.map((_, i) => p.items.length > 1
          ? `<button class="btn ghost" data-approve="${i}">${i + 1}번만</button>` : "").join("")}
        <button class="btn ghost" data-reject="1">아니요</button>
      </div>`;
  }
}

/* 승인 버튼의 유효 시간. 서버가 pending.expires_in(초)을 주면 그 시각에 버튼을
   거두고 알립니다. 서버도 같은 시각부터 승인을 거절하므로, 여기서 거두지 않으면
   사용자가 눌러도 "확인 시간이 지났습니다" 만 돌아옵니다. */
let pendingTimer = null;
function armPendingExpiry(p) {
  clearTimeout(pendingTimer); pendingTimer = null;
  if (!p || p.expires_in == null) return;
  pendingTimer = setTimeout(() => {
    if (S.pending !== p) return;
    S.pending = null;
    S.messages.push({ role: "bot", text: "확인 시간이 지나 승인 버튼을 거두었습니다. 필요하시면 다시 요청해 주세요.", trace: [] });
    renderDock();
  }, Math.max(0, p.expires_in) * 1000 + 500);
}

async function send(text) {
  if ((!text.trim() && !S.imageQuery) || S.busy || S.imageUploading) return;
  const attached = S.imageQuery;
  const requestText = text.trim() || "이 사진과 비슷한 상품을 찾아줘";
  S.messages.push({ role: "me", text: attached ? `📎 사진 첨부\n${requestText}` : requestText });
  S.busy = true; renderDock();
  let r;
  try {
    r = await api.chat(requestText, attached?.query_image_id || null);
  } catch (err) {
    // 서버가 500 을 내거나 연결이 끊기면 여기로 옵니다. 예전에는 이 경우
    // S.busy 가 true 로 남아 상담창이 영구히 "생각하는 중" 에 잠겼습니다.
    S.messages.push({ role: "bot", text: `요청을 처리하지 못했습니다. (${err.message})`, trace: [] });
    toast("상담 요청이 실패했습니다", true);
    return;
  } finally {
    S.busy = false;
    renderDock();
  }
  const searchResults = r.search_results || [];
  if (attached) {
    URL.revokeObjectURL(attached.preview_url);
    S.imageQuery = null;
    renderImageAttachment();
  }
  // 이 답의 상품 목록 줄을 상세 링크로 바꾸는 데 쓴다 (pickListHTML).
  S.messages.push({ role: "bot", text: r.reply, trace: r.trace, products: searchResults });
  // 상담은 장바구니를 바로 바꿀 수 있다(add_to_cart 는 승인 없이 실행). 예전에는 여기서
  // 아무것도 다시 읽지 않아 헤더 숫자와 열어 둔 장바구니 화면이 그대로였다.
  syncShopViews();
  // 서버가 /api/chat 응답에 pending 을 같이 실어 보냅니다.
  // 따로 물어보지 않고 응답에서 바로 읽습니다.
  S.pending = r.pending || null;
  renderDock();
  if (r.search_note) toast(r.search_note);
  if (r.search_performed) {
    // 그리드를 채우는 Tool (서버 GRID_SEARCH_TOOLS 와 같은 목록).
    // search_product 는 같은 인자로 /api/products 를 다시 불러도 같은 조건이 나온다.
    // 나머지는 그럴 수 없다 — 사진 신호·추천 기준은 /api/products 에 없다. 예전에는
    // "비슷한 상품 추천" 같은 화면 문구를 검색어에 넣어 두어서, 정렬만 바꿔도 그 문구로
    // 키워드 검색이 나가 결과가 비었다. 이제는 결과를 S.chatPick 에 고정하고
    // 정렬·필터를 그 목록 안에서만 적용한다.
    const PICK_LABELS = { search_by_image_and_text: "📎 사진 검색 결과",
                          recommend_similar_products: "비슷한 상품 추천", comparing_info: "비교한 상품" };
    const searchTrace = [...(r.trace || [])].reverse()
      .find((item) => (item.name === "search_product" || item.name in PICK_LABELS) && item.ok);
    const pinned = searchTrace && searchTrace.name in PICK_LABELS;
    // 필터 칩으로 옮기는 것은 글 검색의 인자뿐이다. 추천·비교 인자(product_ids)는 필터가 아니고,
    // 사진 검색은 기준에 못 미치면 비슷한 상품으로 채우므로(backfill) 그 인자를 칩으로 걸면
    // 정렬만 바꿔도 채운 상품이 사라진다. 사진 검색의 조건은 실행 기록의 plan 에서 본다.
    const args = searchTrace?.name === "search_product" ? (searchTrace.args || {}) : {};
    S.chatPick = pinned ? { label: PICK_LABELS[searchTrace.name], rows: searchResults } : null;
    S.searchArgs = pinned ? null : args;
    S.semanticQuery = pinned ? null : args.semantic_query || null;
    S.query = pinned ? "" : args.product_name || args.semantic_query || "";
    S.category = args.category || null;
    S.group = args.group || (S.category
      ? Object.entries(GROUPS).find(([, values]) => values.includes(S.category))?.[0] || null
      : null);
    S.gender = args.gender || null;
    S.color = args.color || null;
    S.sort = pinned ? "recommend" : args.sort || "recommend";
    S.shown = 20;
    $("#searchInput").value = S.query;
    $("#searchClear").hidden = !S.query;
    $("#sortSel").value = S.sort;
    // 상담 검색 결과는 Tool 이 돌려준 목록이 전부다. 총 개수를 결과 수로 맞추지 않으면
    // 이전 카탈로그의 총계(14,595)가 남아 '더 보기 (14,555개 남음)' 이 뜨고, 누르면
    // /api/products 가 SQL 순서로 이어 붙여 에이전트 순위를 망친다.
    nextGridSeq();                     // 그 전에 나간 목록 요청이 이 결과를 덮지 못하게
    S.rows = searchResults;
    S.totalRows = searchResults.length;
    S.shown = Math.min(20, searchResults.length) || 20;
    await go("shop");
    renderCatnav(); renderChips(); renderGrid();
    if (window.innerWidth <= 1180) openDock(false);
  }
}

function renderImageAttachment() {
  const el = $("#imageAttachment");
  el.hidden = !S.imageQuery && !S.imageUploading;
  if (S.imageUploading) {
    el.innerHTML = `<span class="image-uploading">사진을 안전하게 처리하고 있어요…</span>`;
    return;
  }
  if (!S.imageQuery) { el.innerHTML = ""; return; }
  el.innerHTML = `<img src="${esc(S.imageQuery.preview_url)}" alt="첨부 이미지" />
    <span>사진이 첨부됐습니다</span>
    <button type="button" id="imageRemove" aria-label="첨부 취소">&times;</button>`;
}

async function attachImage(file) {
  if (!file) return;
  if (typeof api.uploadImage !== "function") {
    toast("목업 화면에서는 사진 검색을 사용할 수 없습니다", true); return;
  }
  if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
    toast("JPEG, PNG, WebP 사진만 첨부할 수 있습니다", true); return;
  }
  if (file.size > 8 * 1024 * 1024) {
    toast("사진은 8MB 이하여야 합니다", true); return;
  }
  S.imageUploading = true; renderImageAttachment();
  const preview = URL.createObjectURL(file);
  try {
    const result = await api.uploadImage(file);
    if (S.imageQuery) URL.revokeObjectURL(S.imageQuery.preview_url);
    S.imageQuery = { ...result, preview_url: preview };
  } catch (error) {
    URL.revokeObjectURL(preview);
    toast(error.message || "사진 업로드에 실패했습니다", true);
  } finally {
    S.imageUploading = false; renderImageAttachment();
  }
}

function openDock(on = true) {
  document.body.classList.toggle("dock-open", on);
  $("#scrim").hidden = !on || window.innerWidth > 1180;
  if (on) setTimeout(() => $("#chatInput").focus(), 240);
}

/* ============================ 라우팅 ============================ */
/* 주소의 해시로 화면을 표현합니다.
   #/            상품 목록
   #/product/P1  상품 상세
   #/cart        장바구니
   #/orders      주문 내역
   해시를 쓰면 브라우저 뒤로/앞으로 가기가 그대로 동작합니다. */
function toHash(view, arg) {
  if (view === "detail") return "#/product/" + arg;
  if (view === "shop") return "#/";
  return "#/" + view;
}
function fromHash() {
  const parts = (location.hash || "#/").replace(/^#\/?/, "").split("/");
  if (parts[0] === "product" && parts[1]) return { view: "detail", arg: parts[1] };
  if (parts[0] === "cart" || parts[0] === "orders") return { view: parts[0] };
  return { view: "shop" };
}

async function render(view, arg) {
  // 목록을 떠날 때 스크롤 위치를 기억해 두었다가 돌아오면 그 자리로.
  if (S.view === "shop" && view !== "shop") S.scrollY = window.scrollY;
  if (view !== "cart") S.checkout = null;
  S.view = view;
  ["shop", "detail", "cart", "orders"].forEach((v) =>
    $("#view-" + v).hidden = v !== view);
  try {
    if (view === "detail") await renderDetail(arg);
    if (view === "cart") await renderCart();
    if (view === "orders") await renderOrders();
  } catch (err) {
    // 예전에는 여기서 예외가 새어 나가 화면이 빈 채로 남았다 (없는 상품 주소 → 404).
    viewError(view, err);
  }
  if (view === "shop") { renderCatnav(); renderChips(); }
  // 스크롤은 항상 즉시 이동합니다. 부드러운 스크롤을 쓰면 애니메이션이
  // 끝나기 전에 다음 화면이 그려져 복원한 위치를 덮어씁니다.
  let y = 0;
  if (view === "shop") { y = S.scrollY || 0; S.scrollY = 0; }  // 돌아올 때만 소비
  requestAnimationFrame(() => window.scrollTo(0, y));
}

/* 화면 전환은 해시만 바꿉니다. 실제 그리기는 hashchange 가 맡습니다.
   이렇게 해야 링크로 이동하든 뒤로가기로 오든 경로가 하나로 유지됩니다. */
function go(view, arg) {
  const h = toHash(view, arg);
  if (location.hash === h || (!location.hash && h === "#/")) return render(view, arg);
  location.hash = h;
}

window.addEventListener("hashchange", () => {
  const r = fromHash();
  render(r.view, r.arg);
});

/* 헤더 숫자. 장바구니 버튼은 응답에 장바구니를 같이 받으므로(cart) 그것을 쓰고,
   주문 수는 주문이 바뀔 수 있을 때만(orders: true) 다시 받는다 — 주문 목록 전체를
   받아 개수만 세는 요청이라, 장바구니를 누를 때마다 부를 일이 아니다.
   헤더의 숫자일 뿐이라 실패해도 알리지 않는다 (서버가 꺼졌으면 다른 곳에서 알린다). */
async function refreshCounts({ cart = null, orders = true } = {}) {
  try {
    const v = cart || await api.cart();
    $("#cartCount").textContent = v.quantity;
    if (orders) {
      const o = await api.orders();
      $("#ordersCount").textContent = o.length;
    }
  } catch { /* 숫자를 그대로 둔다 */ }
}

/* 화면 밖에서 장바구니·주문이 바뀌었을 수 있을 때 — 상담(에이전트가 add_to_cart 는
   승인 없이 바로 실행한다), 승인 실행, 다른 탭. 헤더 숫자와 지금 보고 있는 화면을 맞춘다. */
function syncShopViews() {
  refreshCounts();
  if (S.view === "cart") renderCart().catch((err) => viewError("cart", err));
  if (S.view === "orders") renderOrders().catch((err) => viewError("orders", err));
}

/* 다른 탭에서 승인·거절했거나 장바구니를 바꿨으면, 이 탭으로 돌아왔을 때 맞춘다.
   대화 말풍선은 건드리지 않는다(이 탭에서 쓰던 흐름을 덮지 않게). 확인 대기만 서버 값으로. */
let lastSync = 0;
async function syncFromServer() {
  if (S.busy || Date.now() - lastSync < 1000) return;
  lastSync = Date.now();
  syncShopViews();
  if (typeof api.pending !== "function") return;
  let p;
  try { p = await api.pending(); } catch { return; }
  if (S.busy) return;
  const key = (x) => JSON.stringify(x?.items?.map((i) => i.key) || null);
  if (key(p) !== key(S.pending)) {
    if (S.pending && !p) {
      S.messages.push({ role: "bot", trace: [],
        text: "확인 대기가 이미 끝나(다른 창에서 처리했거나 시간이 지나) 승인 버튼을 거두었습니다." });
    }
    S.pending = p || null;
    renderDock();
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") syncFromServer();
});
window.addEventListener("focus", syncFromServer);

/* 버튼 처리 중 나는 예외(서버 오류·연결 끊김)는 여기서 한 번에 알린다. 예전에는
   조용히 콘솔에만 남아서, 누른 버튼이 아무 반응 없는 것처럼 보였다. */
window.addEventListener("unhandledrejection", (e) => {
  toast(e.reason?.message || "요청을 처리하지 못했습니다", true);
});

/* 새로고침 뒤 이어 그리기. 대화·확인 대기는 서버 세션 DB 에 남아 있다.
   이것을 읽지 않으면 승인 버튼이 사라지고, 서버는 그 확인이 끝나기 전까지
   주문 취소·반품을 막는다 — 사용자에게는 끝낼 버튼이 없는 막다른 길이 된다. */
async function restoreSession() {
  if (typeof api.session !== "function") return;
  let st;
  try { st = await api.session(); } catch { return; }
  // 응답이 오기 전에 사용자가 이미 대화를 시작했으면 그 화면을 덮지 않는다.
  if (S.busy || S.messages.length > 1) return;
  if (st.messages?.length) {
    S.messages = [S.messages[0],
      { role: "bot", text: "이전 대화를 이어서 보여드려요.", trace: [], note: true },
      ...st.messages.map((m) => ({ ...m, trace: [] }))];
  }
  S.pending = st.pending || null;
  renderDock();
  if (S.pending) openDock(true);
}

/* ============================ 이벤트 ============================ */
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-nav],[data-group],[data-cat],[data-gender],[data-color],"
    + "[data-clearq],[data-open],[data-quick],[data-wish],[data-hero],[data-hero-go],"
    + "[data-hero-act],[data-size],[data-color-pick],[data-detail-add],[data-detail-wish],[data-pick],"
    + "[data-q],[data-del],[data-sample],[data-approve],[data-reject],[data-tag],[data-arg],"
    + "[data-order-act],[data-clearpick],[data-retry-grid],[data-home],"
    + "[data-switch-user],[data-reset-user],[data-show-grid],[data-color-set],"
    + "[data-cond-edit],[data-cond-size],[data-clear-query]");
  if (!t) return;
  const d = t.dataset;

  if (d.condEdit) {
    // 조건 칩 → 고치기 팝업 (같은 칩을 다시 누르면 닫는다)
    const pop = t.parentElement.querySelector(".cond-pop");
    const open = pop.hidden;
    closeCondPops(); toggleColorPop(false);
    if (open) {
      pop.innerHTML = condPopHTML(d.condEdit); pop.hidden = false;
      pop.querySelector("input")?.focus();
    }
    return;
  }
  if (d.condSize) {
    S.searchArgs = { ...(S.searchArgs || {}), size: d.condSize };
    applyCond(); return;
  }
  if (d.clearQuery) {
    // 검색어 하나만 푼다. 가격·사이즈 같은 다른 상담 조건은 그대로.
    S.query = ""; S.semanticQuery = null;
    if (S.searchArgs) { delete S.searchArgs.semantic_query; delete S.searchArgs.product_name; }
    $("#searchInput").value = ""; $("#searchClear").hidden = true;
    applyCond(); return;
  }
  if ("colorSet" in d) {
    // 색을 바꾸면 지금 조건(분류·성별·상담 조건·검색어) 그대로 색만 바꿔 서버에 다시 묻는다.
    // 상담의 사진·추천 결과(chatPick)를 보고 있으면 그 목록 안에서 거른다.
    S.color = d.colorSet || null; S.shown = 20;
    toggleColorPop(false); renderChips(); loadGrid(); return;
  }
  if (d.showGrid) {
    // 채팅 목록의 "전체 N개 보기". 그리드가 아직 그 답의 결과를 보여 주고 있으면 그리로 옮기기만
    // 하고, 그사이 다른 검색을 했으면 그 답의 결과(순위 그대로)를 다시 띄운다.
    const rows = S.messages[Number(d.showGrid)]?.products || [];
    if (!rows.length) return;
    const showing = rows.every((p, k) => S.rows[k]?.id === p.id);
    if (!showing) {
      clearChatSearch();
      S.group = null; S.category = null; S.gender = null; S.color = null;
      S.sort = "recommend"; $("#sortSel").value = "recommend";
      S.chatPick = { label: "상담 검색 결과", rows };
      nextGridSeq();
      S.rows = rows; S.totalRows = rows.length; S.shown = Math.min(20, rows.length) || 20;
    }
    if (t.closest("#dock") && window.innerWidth <= 1180) openDock(false);
    await go("shop");
    renderCatnav(); renderChips(); renderGrid();
    // render 가 목록 스크롤을 복원한 뒤(다음 프레임)에 그리드 머리로 옮긴다.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const top = $("#filterChips").getBoundingClientRect().top + window.scrollY - 90;
      window.scrollTo(0, Math.max(0, top));
    }));
    return;
  }
  if ("switchUser" in d) {
    const target = d.switchUser || null;               // "" = 새 익명 사용자
    if (target && target === USER?.current) { toggleUserPop(false); return; }
    if (S.busy) { toast("상담 답을 기다리는 중에는 사용자를 바꿀 수 없습니다", true); return; }
    await api.switchUser(target);
    restart(); return;
  }
  if (d.resetUser) {
    toggleUserPop(false);
    if (S.busy) { toast("상담 답을 기다리는 중에는 초기화할 수 없습니다", true); return; }
    // 되돌릴 수 없는 작업이라 한 번 더 묻는다.
    if (!confirm(`${userLabel()}의 장바구니·대화·승인 대기·찜을 비우고 주문을 처음 상태로 되돌립니다`
      + `${USER?.is_demo ? "" : "(익명 사용자는 주문 없음)"}.\n계속할까요?`)) return;
    await api.resetUser();
    S.wish.clear();                    // 서버 초기화가 이 사용자의 찜도 지운다
    restart(); return;
  }
  if (d.home) {
    // 로고 = 처음 화면. 검색·필터·정렬·상담 결과 고정을 모두 풀고 맨 위 전체 목록으로.
    // 장바구니·주문·대화는 사용자의 것이라 건드리지 않는다.
    e.preventDefault();
    resetBrowse();
    await go("shop");
    renderCatnav(); renderChips(); loadGrid();
    requestAnimationFrame(() => window.scrollTo(0, 0));
    return;
  }
  if (d.nav) return go(d.nav);
  if (d.hero) { goHero(heroIx + Number(d.hero)); startHero(); return; }
  if (d.heroGo) { goHero(Number(d.heroGo)); startHero(); return; }
  if (d.heroAct) {
    if (d.heroAct === "dock") openDock(true);
    else {
      // 인기 상품 = 전체 카탈로그 리뷰순. 상담 결과를 보던 중이면 거기서 나온다.
      clearChatSearch();
      S.sort = "review"; $("#sortSel").value = "review"; S.shown = 20;
      await go("shop"); renderChips(); loadGrid();
    }
    return;
  }
  if ("group" in d) {
    // 상단 대분류를 고르는 것은 "새로 둘러보기" 입니다. 상담 검색에서 넘어온
    // 가격·브랜드·의미 검색어를 여기서 지웁니다. 안 지우면 "15만원 이하 검은색
    // 운동화" 뒤에 "상의" 를 눌렀을 때 15만원 이하 상의만 조용히 나옵니다.
    S.group = d.group || null; S.category = null; S.shown = 20;
    clearChatSearch();          // 색상·성별 칩은 눈에 보이므로 그대로 둔다
    renderCatnav(); renderChips(); await go("shop"); loadGrid(); return;
  }
  if (d.arg) {
    d.arg.split(",").forEach((k) => { if (S.searchArgs) delete S.searchArgs[k]; });
    if (S.searchArgs && !Object.keys(S.searchArgs).length) S.searchArgs = null;
    S.shown = 20; renderChips(); loadGrid(); return;
  }
  if (d.cat) { S.category = S.category === d.cat ? null : d.cat; S.shown = 20; renderChips(); loadGrid(); return; }
  if (d.gender) { S.gender = S.gender === d.gender ? null : d.gender; S.shown = 20; renderChips(); loadGrid(); return; }
  if (d.color) { S.color = null; S.shown = 20; renderChips(); loadGrid(); return; }
  if (d.clearq) { clearChatSearch(); S.shown = 20; renderChips(); loadGrid(); return; }
  if (d.retryGrid) { loadGrid(); return; }
  if (d.clearpick) {
    // 상담 결과에서 나와 일반 목록으로. 고른 성별·색 칩은 눈에 보이므로 그대로 둔다.
    S.chatPick = null; S.shown = 20; renderChips(); loadGrid(); return;
  }
  if (d.tag) {
    S.query = d.tag; $("#searchInput").value = d.tag; $("#searchClear").hidden = false;
    S.shown = 20; await go("shop"); renderChips(); loadGrid(); return;
  }
  if (d.open) {
    // 상담창 안의 상품을 눌렀는데 상담창이 화면을 덮는 폭이면 닫아야 상세가 보인다.
    if (t.closest("#dock") && window.innerWidth <= 1180) openDock(false);
    return go("detail", d.open);
  }

  if (d.quick) {
    const p = await api.product(d.quick);
    const colors = p.colors || [];
    // 색이 여럿이면 대신 골라주지 않고 상세로 보냅니다. 사용자가 고르지 않은
    // 색이 장바구니에 들어가는 것이 "바로 담기" 의 편의보다 나쁩니다.
    if (colors.length > 1) {
      toast("색상을 골라 주세요");
      return go("detail", p.id);
    }
    const color = colors[0]?.color ?? null;
    const size = firstSizeInStock(p, color);
    if (!size) return toast("품절된 상품입니다", true);
    const r = await api.addToCart(p.id, size, color, 1);
    toast(r.message, !r.ok); refreshCounts({ cart: r.cart, orders: false }); return;
  }
  if (d.wish || d.detailWish) {
    const id = d.wish || d.detailWish;
    const on = !S.wish.has(id);
    if (t.disabled) return;
    t.disabled = true;                 // 연달아 눌러 켜고 끄기가 엇갈리지 않게
    try {
      const r = await api.setWish(id, on);
      r.wished ? S.wish.add(id) : S.wish.delete(id);
      // 상세에서는 버튼 글자만 바꾼다. renderDetail 로 다시 그리면 고른 색·사이즈가 풀린다.
      applyWish(id, r.wish_count);
      toast(r.wished ? "찜했습니다" : "찜을 해제했습니다");
    } catch (err) {
      toast(err.message || "찜을 바꾸지 못했습니다", true);
    } finally {
      t.disabled = false;
    }
    return;
  }
  if (d.colorPick) {
    S.detailColor = d.colorPick;
    // 고른 색에 없는 사이즈가 남아 있으면 안 됩니다 — 담기 버튼이 품절을 부릅니다.
    const sizes = sizesOf(S.detailProduct, S.detailColor);
    if (!S.detailSize || !(sizes[S.detailSize] > 0)) {
      S.detailSize = firstSizeInStock(S.detailProduct, S.detailColor);
    }
    $("#pickWrap").innerHTML = pickerHTML(S.detailProduct);
    // 그림도 고른 색으로 다시 그립니다. 색을 골랐는데 그림이 그대로면
    // 무엇을 담는 것인지 화면이 거짓말을 합니다.
    // 사진이 있는 상품은 색이 하나뿐이라(사진에 있는 그 색) 사진이 그대로 맞다.
    $("#dtArt").innerHTML = pic({ ...S.detailProduct, color: S.detailColor }, { full: true });
    return;
  }
  if (d.size) {
    S.detailSize = d.size;
    $$("#szGrid .sz").forEach((b) => b.classList.toggle("on", b.dataset.size === d.size));
    return;
  }
  if (d.detailAdd) {
    if (!S.detailSize) return toast("사이즈를 골라 주세요", true);
    const r = await api.addToCart(d.detailAdd, S.detailSize, S.detailColor, 1);
    toast(r.message, !r.ok); refreshCounts({ cart: r.cart, orders: false }); return;
  }
  if (d.pick) {
    S.checkout = null;                 // 대상이 바뀌면 확인 단계를 닫는다
    S.picked.has(d.pick) ? S.picked.delete(d.pick) : S.picked.add(d.pick);
    renderCart(); return;
  }
  if (d.q) {
    S.checkout = null;
    const { id, color, size } = parseLineKey(d.k);
    try {
      const v = await api.cart();
      const line = v.lines.find((l) => lineKey(l) === d.k);
      if (line) {                                  // 없으면 그 사이 사라진 줄 — 다시 그리기만
        const r = await api.setQuantity(id, size, color, line.quantity + Number(d.q));
        // 재고를 넘기면 서버가 ok:false 와 이유를 준다. 예전에는 이것을 보지 않아서
        // + 를 눌러도 숫자가 그대로인 이유를 알 수 없었다.
        if (r && r.ok === false) toast(r.message, true);
      }
    } catch (err) {
      toast(err.message || "수량을 바꾸지 못했습니다", true);
    }
    // 성공이든 실패든 서버 상태로 다시 그린다. 실패했을 때 화면만 옛 상태로 남지 않게.
    // renderCart 가 헤더 숫자도 맞춘다.
    renderCart().catch((err) => viewError("cart", err)); return;
  }
  if (d.del) {
    S.checkout = null;
    const { id, color, size } = parseLineKey(d.del);
    try {
      const r = await api.removeFromCart(id, size, color);
      S.picked.delete(d.del); S.seenLines.delete(d.del);
      toast(r.message, r.ok === false);
    } catch (err) {
      toast(err.message || "빼지 못했습니다", true);
    }
    renderCart().catch((err) => viewError("cart", err)); return;
  }
  if (d.sample) { openDock(true); send(d.sample); return; }
  if (d.approve) {
    const p = S.pending; if (!p) return;
    const keys = d.approve === "all" ? p.items.map((i) => i.key) : [p.items[Number(d.approve)].key];
    if (S.busy) return;
    S.busy = true; renderDock();
    try {
      const r = await api.approve(keys);
      S.pending = r.pending || null;
      S.messages.push({ role: "bot", text: r.reply, trace: r.trace });
    } catch (err) {
      // 승인 요청이 실패하면 대기 항목은 그대로 두고 알립니다.
      // 실행됐는지 확인되지 않은 상태라 버튼을 없애면 안 됩니다.
      S.messages.push({ role: "bot", text: `승인 요청을 처리하지 못했습니다. (${err.message})`, trace: [] });
      toast("승인 요청이 실패했습니다", true);
    } finally {
      S.busy = false; renderDock();
    }
    syncShopViews();
    return;
  }
  if (d.orderAct) {
    // 주문 화면의 취소·반품 버튼 → 서버가 확인 대기를 열고, 승인은 상담창에서.
    if (S.busy) return;
    S.busy = true; renderDock();
    try {
      const r = await api.orderAction(d.orderId, d.orderAct);
      S.pending = r.pending || null;
      S.messages.push({ role: "bot", text: r.reply, trace: r.trace || [] });
      openDock(true);
    } catch (err) {
      toast("요청을 처리하지 못했습니다", true);
    } finally {
      S.busy = false; renderDock();
    }
    return;
  }
  if (d.reject) {
    if (S.busy) return;
    S.busy = true; renderDock();       // 연달아 눌러 거절이 두 번 나가지 않게
    try {
      const r = await api.reject();
      S.pending = r.pending || null;
      S.messages.push({ role: "bot", text: r.reply, trace: [] });
    } catch (err) {
      toast("요청이 실패했습니다", true);
    } finally {
      S.busy = false; renderDock();
    }
    return;
  }
});

/* id 로 잡는 버튼들.
   버튼 안에 아이콘·글자가 들어 있으면 e.target 이 그 자식이 되므로
   반드시 closest 로 버튼 자체를 찾아야 합니다. (상담 버튼이 안 눌리던 원인) */
document.addEventListener("click", async (e) => {
  const hit = (id) => e.target.closest("#" + id);
  if (hit("userBtn")) { toggleUserPop($("#userPop").hidden); return; }
  if (hit("colorBtn")) { toggleColorPop($("#colorPop").hidden); return; }
  if (!e.target.closest(".color-pick") && $("#colorPop") && !$("#colorPop").hidden) toggleColorPop(false);
  if (!e.target.closest(".cond")) closeCondPops();
  if (!hit("userMenu") && !$("#userPop").hidden) toggleUserPop(false);
  if (hit("pickAll")) {
    S.checkout = null;
    const v = await api.cart();
    // 줄의 열쇠는 renderCart 와 같은 lineKey (상품|색|사이즈). 예전에는 여기만 색이 빠진
    // 옛 열쇠를 써서 picked 와 한 번도 맞지 않았고, "전체 해제" 가 동작하지 않았다.
    const all = v.lines.map(lineKey);
    if (all.every((k) => S.picked.has(k))) S.picked.clear();
    else all.forEach((k) => S.picked.add(k));
    renderCart(); return;
  }
  if (hit("buyAll") || hit("buyPicked")) {
    // 바로 결제하지 않고 금액 확인 단계를 연다.
    S.checkout = { mode: hit("buyAll") ? "all" : "picked", total: null };
    renderCart(); return;
  }
  if (hit("checkoutCancel")) { S.checkout = null; renderCart(); return; }
  if (hit("checkoutGo")) {
    if (!S.checkout) return;
    const btn = hit("checkoutGo"); btn.disabled = true;
    const v = await api.cart();
    const lines = checkoutLines(v, S.checkout.mode);
    // 확인 화면을 띄운 뒤 상담이나 다른 탭에서 장바구니가 바뀌었으면, 본 금액과 다른
    // 금액이 결제된다. 다시 보여 주고 한 번 더 누르게 한다.
    if (!lines.length || linesTotal(lines) !== S.checkout.total) {
      toast("장바구니가 바뀌었습니다. 금액을 다시 확인해 주세요", true);
      renderCart(); return;
    }
    const items = S.checkout.mode === "all" ? null
      : lines.map((l) => ({ product_id: l.product_id, size: l.size, color: l.color }));
    let r;
    try { r = await api.checkout(items); }
    catch (err) { btn.disabled = false; toast(err.message || "결제하지 못했습니다", true); return; }
    toast(r.ok ? `주문 완료 · ${won(r.total)}` : r.message, !r.ok);
    S.checkout = null;
    // 주문된 줄은 사라지므로 선택 기록도 함께 비웁니다. 남은 줄은 다음 렌더에서
    // 새 줄로 취급되어 다시 기본 선택됩니다.
    if (r.ok) { S.picked.clear(); S.seenLines.clear(); }
    renderCart(); refreshCounts();
    return;
  }
  if (hit("moreBtn")) {
    if (S.shown >= S.rows.length && S.rows.length < S.totalRows) await loadGrid(true);
    S.shown = Math.min(S.shown + 20, S.rows.length);
    renderGrid();
  }
  if (hit("imageAttach")) { $("#imageFile").click(); return; }
  if (hit("imageRemove")) {
    if (S.imageQuery) URL.revokeObjectURL(S.imageQuery.preview_url);
    S.imageQuery = null; renderImageAttachment(); return;
  }
  if (hit("dockToggle")) openDock(!document.body.classList.contains("dock-open"));
  if (hit("dockClose") || hit("scrim")) openDock(false);
  if (hit("searchClear")) { clearChatSearch(); S.shown = 20; renderChips(); loadGrid(); }
});

$("#searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#searchInput").value.trim();
  S.semanticQuery = null; S.searchArgs = null; S.chatPick = null; S.shown = 20;
  const typed = text ? typedFilter(text) : null;
  // 헤더 검색은 항상 전체 카탈로그에서 찾는다. 보던 대분류·소분류를 그대로 두면
  // "운동화" 를 보다가 "린넨 셔츠" 를 쳤을 때 운동화 안에서 찾아 신발이 나왔다.
  // 좁히는 것은 검색 뒤 칩으로 한다. 성별·색 칩은 눈에 보이므로 그대로 둔다.
  S.group = typed?.group || null;
  S.category = typed?.category || null;
  if (typed?.brand) {
    S.searchArgs = { brand: typed.brand }; S.query = "";   // 브랜드 칩(× 로 풀 수 있다)
  } else {
    S.query = typed ? "" : text;   // 분류 이름이면 메뉴·칩으로, 그 밖의 글은 의미 검색(KURE)
  }
  $("#searchInput").value = S.query;
  $("#searchClear").hidden = !S.query;
  await go("shop"); renderCatnav(); renderChips(); loadGrid();
});
/* 조건 고치기 팝업의 적용 (가격·검색어). 엔터로도 된다. */
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-cond-form]");
  if (!form) return;
  e.preventDefault();
  const kind = form.dataset.condForm;
  if (kind === "price") {
    const num = (v) => (v === "" ? null : Math.max(0, Math.round(Number(v))));
    let min = num(form.elements.min.value), max = num(form.elements.max.value);
    if (min != null && max != null && min > max) [min, max] = [max, min];
    const next = { ...(S.searchArgs || {}) };
    delete next.min_price; delete next.max_price;
    if (min) next.min_price = min;
    if (max != null) next.max_price = max;
    S.searchArgs = next;
  } else if (kind === "query") {
    const q = form.elements.q.value.trim();
    S.query = q; S.semanticQuery = q || null;          // 고친 검색어는 의미 검색으로 찾는다
    if (S.searchArgs) { delete S.searchArgs.product_name; if (q) S.searchArgs.semantic_query = q; else delete S.searchArgs.semantic_query; }
    $("#searchInput").value = q; $("#searchClear").hidden = !q;
  }
  applyCond();
});
$("#searchInput").addEventListener("input", (e) => {
  $("#searchClear").hidden = !e.target.value;
});
$("#sortSel").addEventListener("change", (e) => {
  S.sort = e.target.value; S.shown = 20; loadGrid();
});
$("#chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#chatInput").value; $("#chatInput").value = "";
  send(v);
});
$("#imageFile").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  e.target.value = "";
  await attachImage(file);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#userPop").hidden) toggleUserPop(false);
  if (e.key === "Escape" && $("#colorPop") && !$("#colorPop").hidden) toggleColorPop(false);
  if (e.key === "Escape") closeCondPops();
});
window.addEventListener("resize", () => {
  $("#scrim").hidden = !document.body.classList.contains("dock-open") || window.innerWidth > 1180;
});

/* ============================ 시작 ============================ */
await loadGroups();
await loadUserMenu();
await loadWishlist();        // 카드의 하트(내가 찜했나)를 그리기 전에
renderHero();
renderCatnav();
renderChips();
renderSamples();
renderDock();
loadGrid();
refreshCounts();
if (window.innerWidth > 1180) openDock(true);
restoreSession();

/* 주소에 해시가 있으면 그 화면으로 시작합니다. (새로고침·북마크·공유 링크) */
{
  const r0 = fromHash();
  if (r0.view !== "shop") render(r0.view, r0.arg);
}
