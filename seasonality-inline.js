/* ══════════════════════════════════════════════════════════════
   계절성 차트 — 월별 상세 팝업의 표 위에 함께 표시

   · 외부 라이브러리 없음. SVG를 직접 그립니다.
   · 데이터는 팝업 표(년월 / 매출)에서 직접 읽습니다.
   · build.py 수정 불필요, 전역 변수명 몰라도 됩니다.

   사용법
     A) 대시보드의 스크립트 태그 안에 이 내용을 붙여넣기
     B) 파일로 올린 뒤 script src 로 참조
   ══════════════════════════════════════════════════════════════ */
(function () {
"use strict";

/* ───────────── 스타일 주입 ───────────── */
var CSS = `
.sz-wrap{font-family:'42dot Sans',system-ui,sans-serif;color:#e8edf3;
  border-bottom:1px solid #2a3542;padding:4px 4px 18px;margin-bottom:6px}
.sz-bar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;padding:12px 0 14px}
.sz-lab{font-size:20px;font-weight:800;color:#FFCB05;margin-right:4px}
.sz-chip{background:#1c2531;border:1px solid #2a3542;border-radius:999px;
  font-family:inherit;font-size:18px;font-weight:700;padding:7px 13px;cursor:pointer;
  display:inline-flex;align-items:center;gap:7px;color:#8b97a5}
.sz-chip .d{width:11px;height:11px;border-radius:50%;background:currentColor}
.sz-chip.off{opacity:.3}
.sz-fold{background:#1c2531;border:1px solid #2a3542;border-radius:9px;color:#8b97a5;
  font-family:inherit;font-size:18px;font-weight:700;padding:8px 13px;cursor:pointer}
.sz-plot{position:relative}
.sz-plot svg{width:100%;height:auto;display:block;overflow:visible}
.sz-tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .1s;
  background:#0b1016;border:1px solid #374454;border-radius:9px;padding:10px 13px;
  font-size:18px;line-height:1.45;white-space:nowrap;z-index:5;
  box-shadow:0 6px 20px rgba(0,0,0,.55)}
.sz-tip.on{opacity:1}
.sz-tip b{font-size:19px}
.sz-tip i{display:inline-block;width:10px;height:10px;border-radius:50%;
  margin-right:7px;font-style:normal}
.sz-note{color:#8b97a5;font-size:17px;margin:10px 0 0;line-height:1.5}
.sz-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px;margin-top:14px}
.sz-st{background:#1c2531;border:1px solid #2a3542;border-radius:9px;padding:11px 14px}
.sz-st .k{color:#8b97a5;font-size:16px}
.sz-st .v{font-size:23px;font-weight:800;color:#FFCB05;margin-top:2px}
.sz-st .v.b{color:#5B9BD5}
`;
(function () {
  if (document.getElementById("sz-style")) return;
  var s = document.createElement("style");
  s.id = "sz-style";
  s.textContent = CSS;
  (document.head || document.documentElement).appendChild(s);
})();

/* ───────────── 유틸 ───────────── */
var MON = ["1월","2월","3월","4월","5월","6월","7월","8월","9월","10월","11월","12월"];
var fmt = function (n) { return n == null ? "—" : n.toLocaleString(); };
var esc = function (s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); };

/** 팝업 표에서 년월·매출을 읽는다 */
function readTable(table) {
  var ym = [], rev = [];
  var trs = table.querySelectorAll("tbody tr");
  if (!trs.length) trs = table.querySelectorAll("tr");
  Array.prototype.forEach.call(trs, function (tr) {
    var c = tr.querySelectorAll("td");
    if (c.length < 2) return;
    var m = c[0].textContent.trim().match(/^(\d{4})[-./](\d{1,2})/);
    var v = parseFloat(c[1].textContent.replace(/[,\s]/g, ""));
    if (m && isFinite(v)) {
      ym.push(m[1] + "-" + ("0" + (+m[2])).slice(-2));
      rev.push(v);
    }
  });
  return { ym: ym, rev: rev };
}

function pivot(ym, rev) {
  var o = {};
  ym.forEach(function (s, i) {
    var y = +s.slice(0, 4), m = +s.slice(5, 7);
    if (!o[y]) o[y] = new Array(12).fill(null);
    o[y][m - 1] = rev[i];
  });
  return o;
}

/** 연평균=100 정규화. 성장 트렌드를 걷어내 계절 모양만 남긴다 */
function toIdx(a) {
  var v = a.filter(function (x) { return x != null; });
  if (!v.length) return a.slice();
  var mu = v.reduce(function (x, y) { return x + y; }, 0) / v.length;
  return a.map(function (x) { return x == null ? null : +(x / mu * 100).toFixed(1); });
}

var isFull = function (a) { return a.every(function (x) { return x != null; }); };

/** 완전연도들의 계절지수 평균 → 성수기·비수기·진폭 */
function profile(by) {
  var full = Object.keys(by).filter(function (y) { return isFull(by[y]); });
  if (full.length < 2) return null;
  var avg = new Array(12).fill(0);
  full.forEach(function (y) {
    toIdx(by[y]).forEach(function (v, i) { avg[i] += v / full.length; });
  });
  var hi = 0, lo = 0;
  avg.forEach(function (v, i) { if (v > avg[hi]) hi = i; if (v < avg[lo]) lo = i; });
  return { avg: avg, hi: hi, lo: lo, amp: avg[hi] - avg[lo], n: full.length };
}

/* 최신 연도일수록 밝고 굵게, 오래될수록 회색으로 물러난다 */
function styleOf(y, latest) {
  return [
    { c: "#FFCB05", w: 3.4 },
    { c: "#ED7D31", w: 2.7 },
    { c: "#5B9BD5", w: 2.2 },
    { c: "#7a8a99", w: 1.7 },
    { c: "#4a5560", w: 1.5 }
  ][Math.min(latest - y, 4)];
}

function niceTicks(lo, hi, n) {
  var span = hi - lo || 1;
  var raw = span / n;
  var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
  var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; })
    .find(function (s) { return s >= raw; }) || mag * 10;
  var out = [], v = Math.ceil(lo / step) * step;
  for (; v <= hi + step * 0.001; v += step) out.push(+v.toFixed(6));
  return out;
}

/* ───────────── SVG 차트 ───────────── */
var W = 880, H = 300, PL = 78, PR = 16, PT = 16, PB = 36;
var IW = W - PL - PR, IH = H - PT - PB;
var X = function (m) { return PL + IW * m / 11; };

function buildChart(by, off, mode) {
  var years = Object.keys(by).map(Number).sort(function (a, b) { return a - b; })
    .filter(function (y) { return off.indexOf(y) < 0; });
  if (!years.length) return null;
  var latest = Math.max.apply(null, Object.keys(by).map(Number));
  var idx = mode === "idx";

  var series = years.map(function (y) {
    return {
      y: y,
      d: idx ? toIdx(by[y]) : by[y].slice(),
      full: isFull(by[y]),
      st: styleOf(y, latest)
    };
  });

  var all = [];
  series.forEach(function (s) {
    s.d.forEach(function (v) { if (v != null) all.push(v); });
  });
  if (!all.length) return null;

  var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  var pad = (hi - lo) * 0.14 || hi * 0.1;
  lo = Math.max(0, lo - pad); hi = hi + pad;
  var Y = function (v) { return PT + IH * (1 - (v - lo) / (hi - lo)); };

  var g = [];

  // 가로 격자 + y축 라벨
  niceTicks(lo, hi, 4).forEach(function (t) {
    var yy = Y(t).toFixed(1);
    g.push('<line x1="' + PL + '" y1="' + yy + '" x2="' + (W - PR) + '" y2="' + yy +
           '" stroke="#232d38" stroke-width="1"/>');
    g.push('<text x="' + (PL - 10) + '" y="' + (+yy + 7) +
           '" text-anchor="end" font-size="20" fill="#8b97a5">' +
           (idx ? t : (t / 1000).toLocaleString() + "k") + "</text>");
  });

  // 계절지수 모드의 기준선 100
  if (idx && lo < 100 && hi > 100) {
    g.push('<line x1="' + PL + '" y1="' + Y(100).toFixed(1) + '" x2="' + (W - PR) +
           '" y2="' + Y(100).toFixed(1) + '" stroke="#4a5560" stroke-width="1.5" stroke-dasharray="3 4"/>');
  }

  // x축 라벨
  MON.forEach(function (m, i) {
    g.push('<text x="' + X(i).toFixed(1) + '" y="' + (H - PB + 26) +
           '" text-anchor="middle" font-size="20" fill="#8b97a5">' + m + "</text>");
  });

  // 연도별 선 (null 구간은 끊는다)
  series.forEach(function (s) {
    var segs = [], cur = [];
    s.d.forEach(function (v, i) {
      if (v == null) { if (cur.length) segs.push(cur); cur = []; }
      else cur.push(X(i).toFixed(1) + "," + Y(v).toFixed(1));
    });
    if (cur.length) segs.push(cur);
    segs.forEach(function (seg) {
      if (seg.length < 2) return;
      g.push('<polyline points="' + seg.join(" ") + '" fill="none" stroke="' + s.st.c +
             '" stroke-width="' + s.st.w + '" stroke-linejoin="round" stroke-linecap="round"' +
             (s.full ? "" : ' stroke-dasharray="7 5"') + "/>");
    });
    s.d.forEach(function (v, i) {
      if (v == null) return;
      g.push('<circle cx="' + X(i).toFixed(1) + '" cy="' + Y(v).toFixed(1) +
             '" r="' + (s.y === latest ? 4.5 : 3) + '" fill="' + s.st.c + '"/>');
    });
  });

  // 호버용 세로선 + 투명 히트박스
  g.push('<line class="sz-vl" x1="0" y1="' + PT + '" x2="0" y2="' + (PT + IH) +
         '" stroke="#FFCB05" stroke-width="1" opacity="0"/>');
  for (var i = 0; i < 12; i++) {
    g.push('<rect class="sz-hit" data-m="' + i + '" x="' + (X(i) - IW / 22).toFixed(1) +
           '" y="' + PT + '" width="' + (IW / 11).toFixed(1) + '" height="' + IH +
           '" fill="transparent"/>');
  }

  return {
    svg: '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid meet">' +
         g.join("") + "</svg>",
    series: series, idx: idx
  };
}

/* ───────────── 설치 ───────────── */
function install(table) {
  if (table.dataset.szDone) return;

  // 툴바(CSV 복사 버튼)를 기준점으로 삼는다
  var anchor = null, p = table.parentElement;
  for (var i = 0; i < 6 && p; i++, p = p.parentElement) {
    anchor = Array.prototype.slice.call(p.querySelectorAll("button,a,div,span"))
      .filter(function (b) { return /CSV/i.test(b.textContent) && b.children.length === 0; })[0];
    if (anchor) break;
  }
  if (!anchor) return;
  table.dataset.szDone = "1";

  var wrap = table.parentElement;

  var box = document.createElement("div");
  box.className = "sz-wrap";
  box.innerHTML =
    '<div class="sz-bar">' +
      '<span class="sz-lab">계절성</span>' +
      '<div style="flex:1"></div>' +
      '<span class="sz-years"></span>' +
      '<button class="sz-fold">접기</button>' +
    "</div>" +
    '<div class="sz-body">' +
      '<div class="sz-plot"><div class="sz-tip"></div></div>' +
      '<p class="sz-note"></p>' +
      '<div class="sz-stats"></div>' +
    "</div>";
  wrap.insertAdjacentElement("beforebegin", box);   // 표 바로 위

  var st = { mode: "amt", off: [], by: null };  // 금액 고정
  var plot = box.querySelector(".sz-plot");
  var tip = box.querySelector(".sz-tip");

  function reload() {
    var d = readTable(table);
    st.by = pivot(d.ym, d.rev);
  }

  function chips() {
    var ys = Object.keys(st.by).map(Number).sort(function (a, b) { return b - a; });
    var latest = ys[0];
    box.querySelector(".sz-years").innerHTML = ys.map(function (y) {
      var s = styleOf(y, latest);
      return '<button class="sz-chip' + (st.off.indexOf(y) < 0 ? "" : " off") +
        '" data-y="' + y + '" style="color:' + s.c + '">' +
        '<span class="d"></span><span style="color:#e8edf3">' + y + "</span></button>";
    }).join(" ");
    Array.prototype.forEach.call(box.querySelectorAll(".sz-chip"), function (b) {
      b.onclick = function () {
        var y = +b.dataset.y, k = st.off.indexOf(y);
        if (k < 0) st.off.push(y); else st.off.splice(k, 1);
        b.classList.toggle("off");
        draw();
      };
    });
  }

  function draw() {
    if (!st.by || !Object.keys(st.by).length) {
      plot.innerHTML = '<div class="sz-tip"></div>';
      box.querySelector(".sz-note").textContent =
        "표에서 년월·매출을 읽지 못했습니다. 첫 열이 YYYY-MM 형식인지 확인하세요.";
      return;
    }
    var c = buildChart(st.by, st.off, st.mode);
    if (!c) { plot.innerHTML = '<div class="sz-tip"></div>'; return; }

    plot.innerHTML = c.svg;
    plot.appendChild(tip);

    box.querySelector(".sz-note").textContent =
      "금액(백만 NTD)입니다. 점선은 12개월이 안 찬 연도입니다. 하단 지표는 완전연도만 사용합니다.";

    // 호버
    var vl = plot.querySelector(".sz-vl");
    Array.prototype.forEach.call(plot.querySelectorAll(".sz-hit"), function (r) {
      r.addEventListener("mouseenter", function () {
        var m = +r.dataset.m;
        vl.setAttribute("x1", X(m)); vl.setAttribute("x2", X(m));
        vl.setAttribute("opacity", "0.45");
        var rows = c.series.map(function (s) {
          var v = s.d[m];
          return '<div><i style="background:' + s.st.c + '"></i>' + s.y + "년 &nbsp;<b>" +
            (v == null ? "—" : (c.idx ? v.toFixed(1) : fmt(v))) + "</b></div>";
        }).join("");
        tip.innerHTML = "<b>" + MON[m] + "</b>" + rows;
        tip.classList.add("on");
        var pct = (X(m) / W) * 100;
        tip.style.left = Math.min(Math.max(pct, 8), 78) + "%";
        tip.style.top = "6px";
      });
      r.addEventListener("mouseleave", function () {
        vl.setAttribute("opacity", "0");
        tip.classList.remove("on");
      });
    });

    // 통계
    var pr = profile(st.by);
    var S = box.querySelector(".sz-stats");
    if (!pr) {
      S.innerHTML = '<div class="sz-st"><div class="k">계절성 통계</div>' +
        '<div class="v b" style="font-size:19px">완전연도 2개 미만</div></div>';
      return;
    }
    var lvl = pr.amp >= 40 ? "강함" : pr.amp >= 20 ? "보통" : "약함";
    S.innerHTML =
      '<div class="sz-st"><div class="k">성수기</div><div class="v">' +
        MON[pr.hi] + " · " + pr.avg[pr.hi].toFixed(0) + "</div></div>" +
      '<div class="sz-st"><div class="k">비수기</div><div class="v b">' +
        MON[pr.lo] + " · " + pr.avg[pr.lo].toFixed(0) + "</div></div>" +
      '<div class="sz-st"><div class="k">진폭</div><div class="v">' +
        pr.amp.toFixed(0) + "p · " + lvl + "</div></div>" +
      '<div class="sz-st"><div class="k">근거</div><div class="v b">완전연도 ' +
        pr.n + "개</div></div>";
  }

  var fold = box.querySelector(".sz-fold");
  fold.onclick = function () {
    var body = box.querySelector(".sz-body");
    var hidden = body.style.display === "none";
    body.style.display = hidden ? "" : "none";
    fold.textContent = hidden ? "접기" : "펼치기";
  };

  // 좌우 화살표로 종목을 넘기면 표가 바뀌므로 다시 읽는다
  new MutationObserver(function () {
    st.off = [];
    reload(); chips(); draw();
  }).observe(table, { childList: true, subtree: true });

  reload(); chips(); draw();
}

/* ───────────── 팝업 감시 ───────────── */
function scan() {
  Array.prototype.forEach.call(document.querySelectorAll("table"), function (t) {
    var h = t.querySelector("thead");
    if (h && /년월/.test(h.textContent)) install(t);
  });
}

function boot() {
  if (!document.body) { setTimeout(boot, 30); return; }
  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  scan();
  console.log("[계절성] 로드됨 — 종목 팝업을 열면 표 위에 차트가 붙습니다");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
})();
