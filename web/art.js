/* ==========================================================================
   상품 그림 — 사진 없이 SVG 로 "사진처럼" 보이게

   상품마다 그림이 조금씩 다릅니다.
     - 색: 상품의 color 로 칠하고, 왼쪽은 밝게 오른콽은 어둡게(입체감)
     - 소재: 데님은 사선 결, 니트·울은 가로 골, 가죽은 광택, 메시는 점 무늬
     - 배경: 상품 색을 아주 연하게 섞은 스튜디오 톤 + 바닥 그림자
     - 기울기: 상품 ID 로 정해지는 -4~4도. 같은 상품은 늘 같은 각도

   좌표계는 200 x 260. 타일 비율에 맞춰 늘립니다.
   SVG 안의 id(그라데이션·클립)는 한 화면에 그림이 수십 개 있으므로
   상품 ID 를 붙여 서로 겹치지 않게 합니다.
   ========================================================================== */

export const SWATCH = {
  "검은색": "#17171A", "흰색": "#FFFFFF", "회색": "#9C9CA4",
  "네이비": "#20304F", "파란색": "#2A63C8", "하늘색": "#8FC6E9",
  "카키색": "#6B6A48", "갈색": "#7A5334", "베이지색": "#DAC9B0",
  "빨간색": "#C6362C", "분홍색": "#E79BB4", "형광색": "#C6EE38",
  // 아마존 카탈로그로 바꾸며 늘어난 색. 없으면 점·그림이 전부 회색(#CCC)으로 나왔다.
  "금색": "#C9A227", "은색": "#BFC3C8", "노란색": "#F2C94C", "주황색": "#E8812F",
  "초록색": "#3E8E4F", "민트색": "#9FDCC8", "청록색": "#1F8A8A", "보라색": "#7B4FA6",
  "라벤더색": "#B9A6DB", "와인색": "#6E1F32", "코랄색": "#F08A70", "아이보리": "#F4EFDF",
  "차콜": "#3B3C40",
};

/* ------------------------------ 색 계산 ------------------------------ */
function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function rgbToHex([r, g, b]) {
  return "#" + [r, g, b].map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
}
/* t>0 이면 흰색 쪽으로, t<0 이면 검은색 쪽으로 t 만큼 섭니다. */
function mix(hex, t) {
  const target = t > 0 ? 255 : 0;
  const k = Math.abs(t);
  return rgbToHex(hexToRgb(hex).map((v) => v + (target - v) * k));
}
function luminance(hex) {
  const [r, g, b] = hexToRgb(hex);
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
}
function hash(str) {
  let h = 0;
  for (const ch of String(str)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h;
}

/* ------------------------------ 소재 결 ------------------------------ */
/* 소재 이름에 들어 있는 낱말로 고릅니다. 정확히 몰라도 무난한 결이 들어갑니다. */
function textureOf(material = "") {
  if (/데님/.test(material)) return "denim";
  if (/가죽|레더|스웨이드/.test(material)) return "leather";
  if (/니트|울|캐시미어|알파카|플리스/.test(material)) return "knit";
  if (/메시|메쉬|나일론|폴리/.test(material)) return "mesh";
  if (/린넨|면|코튼|옥스포드|포플린/.test(material)) return "linen";
  return "plain";
}

function texturePattern(kind, id, ink) {
  switch (kind) {
    case "denim":
      return `<pattern id="tx-${id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(35)">
        <line x1="0" y1="0" x2="0" y2="6" stroke="${ink}" stroke-width="1.2" opacity=".18"/></pattern>`;
    case "knit":
      return `<pattern id="tx-${id}" width="8" height="5" patternUnits="userSpaceOnUse">
        <path d="M0,2.5 Q2,0 4,2.5 T8,2.5" fill="none" stroke="${ink}" stroke-width="1" opacity=".16"/></pattern>`;
    case "mesh":
      return `<pattern id="tx-${id}" width="5" height="5" patternUnits="userSpaceOnUse">
        <circle cx="2.5" cy="2.5" r=".9" fill="${ink}" opacity=".14"/></pattern>`;
    case "linen":
      return `<pattern id="tx-${id}" width="4" height="4" patternUnits="userSpaceOnUse">
        <path d="M0,2 H4 M2,0 V4" stroke="${ink}" stroke-width=".6" opacity=".10"/></pattern>`;
    case "leather":
      return `<linearGradient id="tx-${id}" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#fff" stop-opacity=".0"/>
        <stop offset=".35" stop-color="#fff" stop-opacity=".22"/>
        <stop offset=".5" stop-color="#fff" stop-opacity=".0"/>
        <stop offset="1" stop-color="#000" stop-opacity=".12"/></linearGradient>`;
    default:
      return "";
  }
}

/* ------------------------------ 품목 모양 ------------------------------ */
/* 각 항목: base = 색을 칠하는 본체 경로(들), detail = 그 위에 얹는 선·단추·주머니.
   detail 의 색은 currentColor(잉크색)와 #fff 를 섞어 쓰고, 본체 그라데이션이
   입체감을 만듭니다. */
const SHAPES = {
  운동화: {
    base: `<path d="M30,150 C48,126 80,118 102,118 L124,96 C150,92 166,110 170,132 L178,150 L178,160 L30,160 Z"/>`,
    detail: `
      <path d="M24,160 L184,160 Q192,160 192,171 L192,180 Q192,190 180,190 L36,190 Q24,190 24,178 Z" fill="#fff" opacity=".92"/>
      <path d="M24,178 Q104,172 192,178" fill="none" stroke="currentColor" stroke-width="2" opacity=".18"/>
      <path d="M40,160 L176,160" stroke="currentColor" stroke-width="2.5" opacity=".28" fill="none"/>
      <path d="M92,128 L110,142 M104,120 L122,134 M116,112 L134,126" stroke="#fff" stroke-width="4.5" opacity=".55" fill="none" stroke-linecap="round"/>
      <path d="M60,150 Q80,136 104,138" stroke="currentColor" stroke-width="2" opacity=".22" fill="none"/>
      <path d="M124,96 L150,124" stroke="#fff" stroke-width="2" opacity=".35" fill="none"/>
      <circle cx="152" cy="140" r="5" fill="#fff" opacity=".7"/>`,
  },
  구두: {
    base: `<path d="M40,164 Q78,152 104,150 L140,118 Q162,114 170,134 L178,160 Q180,174 166,174 L48,174 Q38,174 38,168 Z"/>`,
    detail: `
      <path d="M146,174 L176,174 L176,194 L150,194 Q142,194 142,186 Z" fill="%DARK%"/>
      <path d="M40,170 L178,170" stroke="#fff" stroke-width="3" opacity=".35" fill="none"/>
      <path d="M108,150 Q124,134 142,124" stroke="#fff" stroke-width="2" opacity=".4" fill="none"/>
      <path d="M118,142 L130,150 M126,136 L138,144" stroke="#fff" stroke-width="3" opacity=".45" fill="none" stroke-linecap="round"/>`,
  },
  부츠: {
    base: `<path d="M70,40 Q100,32 130,40 L132,148 L170,164 Q184,172 184,186 L184,198 L68,198 Q62,198 62,190 L66,120 Z"/>`,
    detail: `
      <path d="M62,190 L184,190 L184,198 L62,198 Z" fill="currentColor" opacity=".45"/>
      <path d="M70,80 L130,80 M72,112 L130,112" stroke="#fff" stroke-width="3" opacity=".3" fill="none"/>
      <path d="M72,48 Q100,40 128,48" stroke="#fff" stroke-width="4" opacity=".35" fill="none"/>
      <circle cx="80" cy="160" r="3" fill="#fff" opacity=".5"/><circle cx="80" cy="130" r="3" fill="#fff" opacity=".5"/>`,
  },
  샌들: {
    base: `<path d="M38,166 L170,166 Q184,166 184,180 Q184,194 170,194 L38,194 Q24,194 24,180 Q24,166 38,166 Z"/>`,
    detail: `
      <path d="M24,186 Q104,180 184,186" fill="none" stroke="#fff" stroke-width="2.5" opacity=".45"/>
      <path d="M54,166 Q82,118 114,142" stroke="%DARK%" stroke-width="13" fill="none" stroke-linecap="round"/>
      <path d="M98,166 Q133,126 159,160" stroke="%DARK%" stroke-width="13" fill="none" stroke-linecap="round"/>
      <path d="M56,164 Q82,122 112,144" stroke="#fff" stroke-width="3" fill="none" stroke-linecap="round" opacity=".35"/>`,
  },
  티셔츠: {
    base: `<path d="M60,54 L84,42 Q100,58 116,42 L140,54 L160,86 L138,98 L138,212 L62,212 L62,98 L40,86 Z"/>`,
    detail: `
      <path d="M84,42 Q100,62 116,42" fill="#fff" opacity=".22"/>
      <path d="M62,98 L40,86 M138,98 L160,86" stroke="currentColor" stroke-width="2" opacity=".25" fill="none"/>
      <path d="M66,204 L134,204" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>`,
  },
  니트: {
    base: `<path d="M58,56 L84,44 Q100,58 116,44 L142,56 L164,106 L142,118 L142,216 L58,216 L58,118 L36,106 Z"/>`,
    detail: `
      <path d="M84,44 Q100,60 116,44" fill="#fff" opacity=".2"/>
      <path d="M58,204 L142,204 M58,196 L142,196" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>
      <path d="M36,106 L58,118 M164,106 L142,118" stroke="#fff" stroke-width="2.5" opacity=".35" fill="none"/>`,
  },
  셔츠: {
    base: `<path d="M60,54 L86,42 L100,66 L114,42 L140,54 L160,88 L138,100 L138,214 L62,214 L62,100 L40,88 Z"/>`,
    detail: `
      <path d="M100,70 L100,210" stroke="#fff" stroke-width="2.5" opacity=".5" fill="none"/>
      <path d="M86,42 L100,66 L72,60 Z" fill="#fff" opacity=".3"/>
      <path d="M114,42 L100,66 L128,60 Z" fill="#fff" opacity=".3"/>
      <path d="M62,100 L40,88 M138,100 L160,88" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>
      <circle cx="100" cy="110" r="2.6" fill="#fff" opacity=".75"/><circle cx="100" cy="140" r="2.6" fill="#fff" opacity=".75"/>
      <circle cx="100" cy="170" r="2.6" fill="#fff" opacity=".75"/><circle cx="100" cy="200" r="2.6" fill="#fff" opacity=".75"/>
      <path d="M62,124 L84,124 L84,146 L62,146 Z" fill="#fff" opacity=".12"/>`,
  },
  후드: {
    base: `<path d="M72,50 Q100,22 128,50 Q138,66 100,76 Q62,66 72,50 Z"/>
           <path d="M58,62 L84,50 Q100,74 116,50 L142,62 L164,112 L142,124 L142,218 L58,218 L58,124 L36,112 Z"/>`,
    detail: `
      <path d="M78,52 Q100,86 122,52" fill="currentColor" opacity=".35"/>
      <path d="M72,154 L128,154 L134,190 L66,190 Z" fill="#fff" opacity=".16"/>
      <path d="M72,154 L128,154" stroke="#fff" stroke-width="2" opacity=".35" fill="none"/>
      <path d="M90,96 L88,130 M110,96 L112,130" stroke="#fff" stroke-width="3.5" opacity=".6" fill="none" stroke-linecap="round"/>
      <circle cx="88" cy="132" r="3" fill="#fff" opacity=".7"/><circle cx="112" cy="132" r="3" fill="#fff" opacity=".7"/>
      <path d="M58,206 L142,206" stroke="#fff" stroke-width="3" opacity=".3" fill="none"/>`,
  },
  이너: {
    base: `<path d="M74,52 L88,44 Q100,58 112,44 L126,52 L130,96 L130,198 L70,198 L70,96 Z"/>`,
    detail: `
      <path d="M88,44 Q100,60 112,44" fill="#fff" opacity=".25"/>
      <path d="M72,190 L128,190" stroke="#fff" stroke-width="2" opacity=".35" fill="none"/>
      <path d="M78,60 L76,96 M122,60 L124,96" stroke="#fff" stroke-width="1.5" opacity=".3" fill="none"/>`,
  },
  재킷: {
    base: `<path d="M56,58 L88,42 L100,78 L112,42 L144,58 L166,110 L144,120 L144,222 L56,222 L56,120 L34,110 Z"/>`,
    detail: `
      <path d="M88,42 L100,78 L76,118 L68,60 Z" fill="#fff" opacity=".3"/>
      <path d="M112,42 L100,78 L124,118 L132,60 Z" fill="#fff" opacity=".3"/>
      <path d="M100,78 L100,222" stroke="#fff" stroke-width="2" opacity=".45" fill="none"/>
      <path d="M62,150 L86,150 L86,172 L62,172 Z M114,150 L138,150 L138,172 L114,172 Z" fill="currentColor" opacity=".25"/>
      <path d="M62,150 L86,150 M114,150 L138,150" stroke="#fff" stroke-width="2" opacity=".4" fill="none"/>
      <path d="M56,120 L34,110 M144,120 L166,110" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>
      <path d="M56,214 L144,214" stroke="currentColor" stroke-width="3" opacity=".22" fill="none"/>`,
  },
  코트: {
    base: `<path d="M54,58 L86,42 L100,80 L114,42 L146,58 L168,114 L146,124 L146,238 L54,238 L54,124 L32,114 Z"/>`,
    detail: `
      <path d="M86,42 L100,80 L80,124 L66,60 Z" fill="#fff" opacity=".26"/>
      <path d="M114,42 L100,80 L120,124 L134,60 Z" fill="#fff" opacity=".18"/>
      <path d="M100,80 L100,238" stroke="#fff" stroke-width="2" opacity=".4" fill="none"/>
      <path d="M54,152 L146,152" stroke="currentColor" stroke-width="8" opacity=".25" fill="none"/>
      <path d="M60,176 L88,176 L88,202 L60,202 Z M112,176 L140,176 L140,202 L112,202 Z" fill="#fff" opacity=".12"/>
      <circle cx="88" cy="120" r="4.5" fill="#fff" opacity=".75"/><circle cx="88" cy="186" r="4.5" fill="#fff" opacity=".75"/><circle cx="88" cy="216" r="4.5" fill="#fff" opacity=".75"/>`,
  },
  아웃도어: {
    base: `<path d="M56,58 L86,46 L100,68 L114,46 L144,58 L166,110 L144,120 L144,220 L56,220 L56,120 L34,110 Z"/>`,
    detail: `
      <path d="M100,68 L100,220" stroke="#fff" stroke-width="6" opacity=".5" fill="none"/>
      <path d="M100,68 L100,220" stroke="currentColor" stroke-width="1.5" opacity=".35" fill="none"/>
      <path d="M60,134 L92,134 L92,158 L60,158 Z M108,134 L140,134 L140,158 L108,158 Z" fill="#fff" opacity=".22"/>
      <path d="M60,134 L92,134 M108,134 L140,134" stroke="currentColor" stroke-width="2.5" opacity=".3" fill="none"/>
      <path d="M56,186 L144,186" stroke="#fff" stroke-width="3" opacity=".3" fill="none"/>
      <path d="M86,46 Q100,40 114,46 L118,58 Q100,52 82,58 Z" fill="currentColor" opacity=".3"/>`,
  },
  정장: {
    base: `<path d="M58,56 L88,42 L100,80 L112,42 L142,56 L162,108 L142,118 L142,222 L58,222 L58,118 L38,108 Z"/>`,
    detail: `
      <path d="M88,42 L100,80 L74,116 L68,58 Z" fill="#fff" opacity=".28"/>
      <path d="M112,42 L100,80 L126,116 L132,58 Z" fill="#fff" opacity=".28"/>
      <path d="M92,42 L100,64 L108,42" fill="#fff" opacity=".85"/>
      <path d="M100,64 L96,74 L100,80 L104,74 Z" fill="currentColor" opacity=".7"/>
      <path d="M100,80 L100,222" stroke="#fff" stroke-width="1.5" opacity=".35" fill="none"/>
      <path d="M62,164 L84,164 L84,168 L62,168 Z M116,164 L138,164 L138,168 L116,168 Z" fill="#fff" opacity=".3"/>
      <circle cx="100" cy="150" r="3.5" fill="#fff" opacity=".8"/>`,
  },
  팬츠: {
    base: `<path d="M64,50 L136,50 L142,116 L132,226 L106,226 L100,138 L94,226 L68,226 L58,116 Z"/>`,
    detail: `
      <path d="M64,50 L136,50 L136,60 L64,60 Z" fill="currentColor" opacity=".3"/>
      <path d="M100,60 L100,134" stroke="#fff" stroke-width="2.5" opacity=".35" fill="none"/>
      <path d="M70,66 Q80,84 82,100 M130,66 Q120,84 118,100" stroke="#fff" stroke-width="1.5" opacity=".35" fill="none"/>
      <circle cx="100" cy="56" r="2.5" fill="#fff" opacity=".8"/>
      <path d="M68,218 L94,218 M106,218 L132,218" stroke="#fff" stroke-width="2.5" opacity=".35" fill="none"/>`,
  },
  스커트: {
    base: `<path d="M70,58 L130,58 L158,202 L42,202 Z"/>`,
    detail: `
      <path d="M70,58 L130,58 L130,68 L70,68 Z" fill="currentColor" opacity=".3"/>
      <path d="M94,68 L84,200 M112,68 L128,200 M100,68 L100,200" stroke="#fff" stroke-width="1.5" opacity=".28" fill="none"/>
      <path d="M44,196 L156,196" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>`,
  },
  원피스: {
    base: `<path d="M66,54 L86,42 Q100,58 114,42 L134,54 L140,98 L128,106 L162,216 L38,216 L72,106 L60,98 Z"/>`,
    detail: `
      <path d="M86,42 Q100,60 114,42" fill="#fff" opacity=".22"/>
      <path d="M72,106 L128,106" stroke="currentColor" stroke-width="5" opacity=".25" fill="none"/>
      <path d="M86,110 L74,214 M114,110 L126,214 M100,110 L100,214" stroke="#fff" stroke-width="1.5" opacity=".25" fill="none"/>
      <path d="M40,210 L160,210" stroke="#fff" stroke-width="2" opacity=".3" fill="none"/>`,
  },
};

/* 신발은 그림이 아래쪽 띠에만 있어 작아 보입니다. 카드 안에서 조금 키웁니다. */
const ZOOM = { 운동화: 1.06, 구두: 1.14, 샌들: 1.1, 부츠: 1.0 };
/* 바닥 그림자의 세로 위치. 신발은 그림이 위쪽에 있어 그림자를 바로 밑에 붙인다. */
const FLOOR = { 운동화: 200, 구두: 202, 샌들: 202, 부츠: 206 };

/** 상품 하나의 SVG 마크업. */
export function art(product, { pad = 0 } = {}) {
  const shape = SHAPES[product.category] ||
    { base: '<rect x="62" y="72" width="76" height="116" rx="10"/>', detail: "" };
  const fill = SWATCH[product.color] || "#B9B9C0";
  const id = String(product.id || product.product_id || product.name || "x").replace(/[^A-Za-z0-9_-]/g, "");
  const seed = hash(id + (product.name || ""));
  const tilt = ((seed % 9) - 4) * 0.9;                    // -3.6 ~ 3.6 도
  const bright = luminance(fill) > 0.72;

  // 잉크색: 밝은 옷은 어두운 잉크로 디테일을 그리고, 어두운 옷은 밝은 잉크로.
  const ink = bright ? mix(fill, -0.55) : mix(fill, 0.75);
  const left = mix(fill, bright ? 0.06 : 0.22);           // 빛 받는 쪽
  const right = mix(fill, bright ? -0.16 : -0.3);         // 그늘 쪽
  const outline = bright ? "rgba(14,14,16,.22)" : "rgba(14,14,16,.1)";

  // 배경: 상품 색을 아주 연하게 섭은 스튜디오 톤. 흰 옷은 따뜻한 회색.
  const tint = bright && luminance(fill) > 0.9 ? "#E7E4DF" : mix(fill, 0.86);
  const tint2 = bright && luminance(fill) > 0.9 ? "#F4F2EE" : mix(fill, 0.94);

  // 흰 옷·베이지처럼 밝은 옷에는 흰 디테일이 안 보인다. 그때는 어두운 톤으로 바꿔 그린다.
  const dark = mix(fill, -0.32);                         // 끈·밑창처럼 옷보다 한 톤 어두운 부분
  const detail = (bright ? shape.detail.replaceAll("#fff", mix(fill, -0.38)) : shape.detail)
    .replaceAll("%DARK%", dark);
  const zoom = ZOOM[product.category] || 1;
  const place = `rotate(${tilt} 100 140) translate(100 150) scale(${zoom * (1 - pad / 100)}) translate(${-100 + pad} ${-150 + pad})`;

  const texKind = textureOf(product.material);
  const tex = texturePattern(texKind, id, ink);
  const texFill = texKind === "plain" ? "" :
    `<g clip-path="url(#cp-${id})"><rect width="200" height="260" fill="url(#tx-${id})"/></g>`;

  return `<svg viewBox="0 0 200 260" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
    <defs>
      <linearGradient id="bg-${id}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${tint2}"/><stop offset="1" stop-color="${tint}"/>
      </linearGradient>
      <linearGradient id="body-${id}" x1="0" y1="0" x2="1" y2="0.3">
        <stop offset="0" stop-color="${left}"/><stop offset=".55" stop-color="${fill}"/><stop offset="1" stop-color="${right}"/>
      </linearGradient>
      <radialGradient id="floor-${id}" cx=".5" cy=".5" r=".5">
        <stop offset="0" stop-color="#000" stop-opacity=".22"/><stop offset="1" stop-color="#000" stop-opacity="0"/>
      </radialGradient>
      <clipPath id="cp-${id}"><g transform="${place}">${shape.base}</g></clipPath>
      ${tex}
    </defs>
    <rect width="200" height="260" fill="url(#bg-${id})"/>
    <ellipse cx="102" cy="${FLOOR[product.category] || 236}" rx="74" ry="12" fill="url(#floor-${id})"/>
    <g transform="${place}"
       fill="url(#body-${id})" stroke="${outline}" stroke-width="1.6" stroke-linejoin="round">
      ${shape.base}
    </g>
    ${texFill}
    <g transform="${place}"
       color="${ink}" fill="${ink}" stroke-linejoin="round" stroke-linecap="round">
      ${detail}
    </g>
  </svg>`;
}

export function swatchDot(color) {
  const hex = SWATCH[color] || "#CCC";
  return `<i class="swatch" style="background:${hex}"></i>`;
}
