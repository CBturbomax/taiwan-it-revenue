#!/usr/bin/env python3
"""build.py -- read data.db, emit a single self-contained dashboard.html.

Every metric here is recomputed from the stored absolute figures on each run;
nothing derived is read from or written back to the database.

Units: data.db stores 千元 (thousand NTD) exactly as published. The dashboard
displays 백만 NTD, so values are divided by 1000 at render time and nowhere else.

Sections, top to bottom:
  1. 조기공시분     -- the month still being filed, kept out of every other number
  2. 종목 표        -- sortable + searchable, BoM group tagged, numbers only
  3. Top movers     -- YoY/MoM best and worst, small-revenue names excluded
  4. 그룹 YoY 히트맵 -- BoM group x 24 months, like-for-like YoY
  5. 변곡 점검      -- 역성장 / 시퀀셜 둔화 / 성장 지속
  6. 종목 차트      -- one hand-drawn SVG per BoM stock, grouped by BoM layer
  (렌더 순서는 render() 참조: 차트/히트맵/변곡/조기공시/타임라인/표/movers)

Usage:
    python build.py
    python build.py --db data.db --out dashboard.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bom_groups  # noqa: E402  -- AI-server BoM grouping, see bom_groups.py
import groups  # noqa: E402  -- hand-edited config, see groups.py

# Naming stack, most specific first: hand-picked -> mechanical hanja reading ->
# the Chinese name itself. Both name modules are optional so the dashboard still
# builds on a fresh checkout.
try:
    from names import NAME_KR
except ImportError:
    NAME_KR = {}
try:
    from names_auto import NAME_AUTO
except ImportError:
    NAME_AUTO = {}
# h1 옆 아이콘. 별도 파일로 두면 gh-pages에 index.html만 올라가 깨지므로
# data URI로 인라인한다. 없으면 이모지로 대체한다.
try:
    from logo import LOGO_DATA_URI
except ImportError:
    LOGO_DATA_URI = ""

DB_PATH = os.path.join(HERE, "data.db")
OUT_PATH = os.path.join(HERE, "dashboard.html")

# A month is treated as complete once it holds at least this share of the
# busiest month's row count. Filings are due by the 10th, so the month being
# filed sits far below the line and never contaminates the body of the report.
COMPLETE_RATIO = 0.7

# 빌드 시각은 항상 한국시간으로 찍는다.
# 실행 환경의 로컬 시간을 쓰면 서버(UTC)와 이 PC(KST)의 표기가 9시간 어긋나,
# 어느 쪽이 최신인지 비교할 수 없다 -- 실제로 그 때문에 낡은 로컬 빌드가
# 배포본보다 388분 새롭다고 오판되어 배포됐다.
KST = timezone(timedelta(hours=9))

# 월별 숫자 라벨 크기. 3열 그리드에서 SVG는 약 497px로 그려지고 viewBox는 540
# 고정이라 배율이 약 0.92다. 화면에서 9px로 보이려면 user unit 기준 9.8이어야 한다.
# (CSS 문자열이 이 파일 위쪽에서 f-string으로 평가되므로 여기 있어야 한다)
LBL_FS = 9.8       # 3열에서 약 9 CSS px
LBL_H = 12.0       # 라벨 박스 높이 (user unit)
# 한 달 폭(14.3)보다 라벨이 넓어 이웃과 겹친다. 홀수달을 위아래로 어긋나게 놓아
# 두 줄로 나누면 각 줄의 간격이 28.7이 되어 서로 부딪히지 않는다.
LBL_ZIG = 13.0

BG = "#0f1419"
FG = "#ffffff"
# Korean market convention: rises are red, falls are blue -- the opposite of the
# US. #ED7D31 is the warm end of the given palette, so it carries "up"; #FFCB05
# is reserved for absolute figures so a revenue number never reads as a change.
ACCENT = "#FFCB05"      # absolute values (revenue, maxima)
ACCENT2 = "#5B9BD5"     # negative change / chart bars
ACCENT3 = "#ED7D31"     # positive change / chart YoY line / section headings


# --------------------------------------------------------------------------- #
# metrics -- recomputed every build, never persisted
# --------------------------------------------------------------------------- #
def pct(cur, base):
    """Percent change. None whenever the comparison is not meaningful.

    A missing or non-positive base yields None, not 0 -- a blank cell says
    'no comparable period', which is the honest answer. Returning 0 would read
    as 'flat'.
    """
    if cur is None or base is None or base <= 0:
        return None
    return (cur / base - 1) * 100.0


def shift_ym(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    t = y * 12 + (m - 1) + months
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def yoy_at(rec: dict, ym: str, stats: dict | None = None):
    """YoY% for one month.

    Our own stored month twelve back comes first: it is re-read from MOPS on
    every fetch, so it reflects later restatements. The source's 去年當月營收 is
    frozen at the time that month was filed, and is only used when our window
    does not reach back far enough -- which is every month before 2024-07, since
    the backfill starts at 2023-07.
    """
    cur = rec["rev"].get(ym)
    base = rec["rev"].get(shift_ym(ym, -12))
    if base is None:
        base = rec["rev_ly"].get(ym)
        if base is not None and stats is not None:
            stats["yoy_fallback"] += 1
    return pct(cur, base)


def compute_metrics(rec: dict, ref_ym: str, stats: dict) -> dict:
    """YoY / MoM / cumulative YoY at the reference month."""
    rev, cum = rec["rev"], rec["cum"]
    cur = rev.get(ref_ym)

    yoy = yoy_at(rec, ref_ym, stats)
    if yoy is None:
        stats["yoy_blank"] += 1

    # YoY 가속(%p): 이번 달 YoY가 지난 달 YoY보다 몇 %p 높아졌는가.
    # 성장률의 방향 전환을 잡는 지표라, 둘 중 하나라도 없으면 빈칸으로 둔다.
    yoy_prev = yoy_at(rec, shift_ym(ref_ym, -1))
    accel = None if (yoy is None or yoy_prev is None) else yoy - yoy_prev
    if accel is None:
        stats["accel_blank"] += 1

    pm = rev.get(shift_ym(ref_ym, -1))
    if pm is None:
        pm = rec["rev_last_month_k"]
        if pm is not None:
            stats["mom_fallback"] += 1
    mom = pct(cur, pm)
    if mom is None:
        stats["mom_blank"] += 1

    # 누계YoY uses the cumulative the company itself filed (累計營業收入), taken
    # from this month's row and from the same month a year earlier. Summing our
    # own monthlies was drifting off the filed figure: rounded 千元 monthlies
    # accumulate a unit or two of error, and a 정정공시 that moves revenue
    # between two months leaves the sum right but either month wrong.
    this_cum = cum.get(ref_ym)
    last_cum = cum.get(shift_ym(ref_ym, -12))
    cum_yoy = pct(this_cum, last_cum)
    if cum_yoy is None:
        stats["cum_blank"] += 1
        if last_cum is None:
            stats["cum_no_prior"] += 1

    # transparency only: does last year's stored 累計 agree with the 去年累計營收
    # this month's row reports? Both should describe the same period.
    rep_ly = rec["rev_cum_ly_k"]
    if last_cum is not None and rep_ly is not None:
        d = last_cum - rep_ly
        if d == 0:
            stats["cum_ly_exact"] += 1
        elif abs(d) <= 6:
            stats["cum_ly_rounding"] += 1
        else:
            stats["cum_ly_differ"] += 1
            stats["cum_ly_rows"].append(
                (rec["code"], rec["name"], last_cum, rep_ly, d))

    # NOTE: keys here get merged over the record, so none may collide with the
    # series dicts ("rev", "cum", "rev_ly"). Naming the scalars "cum"/"cum_ly"
    # silently replaced the whole 累計 series with a single month's figure.
    return {"rev_now": cur, "yoy": yoy, "mom": mom, "cum_yoy": cum_yoy,
            "yoy_accel": accel, "yoy_prev": yoy_prev,
            "cum_now": this_cum, "cum_ly_now": last_cum}


# --------------------------------------------------------------------------- #
# data loading
# --------------------------------------------------------------------------- #
def detect_months(conn) -> tuple[str, list[tuple[str, int]]]:
    counts = [(r["ym"], r["n"]) for r in conn.execute(
        "SELECT ym, COUNT(*) n FROM revenue GROUP BY ym ORDER BY ym")]
    if not counts:
        raise SystemExit("data.db has no rows -- run fetch.py first")
    mx = max(n for _, n in counts)
    complete = [ym for ym, n in counts if n >= COMPLETE_RATIO * mx]
    if not complete:
        raise SystemExit("no month looks complete")
    ref = max(complete)
    pending = [(ym, n) for ym, n in counts if ym > ref]
    return ref, pending


def filing_deadline(ym: str) -> tuple[str, bool]:
    """대만 규정: 익월 10일까지. 주말이면 익영업일로 순연.

    Returns ('YYYY-MM-DD', rolled). 대만 공휴일은 반영하지 않는다 -- 국정공휴일
    표를 하드코딩하면 매년 손으로 갱신해야 하고, 갱신을 잊는 순간 조용히 틀린
    날짜를 보여주게 된다. 주말 순연만 계산하고 화면에 그 한계를 밝힌다.
    """
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    d = date(y, m, 10)
    rolled = False
    while d.weekday() >= 5:          # 5=토, 6=일
        d += timedelta(days=1)
        rolled = True
    return d.isoformat(), rolled


def load_disclosure(conn, ym: str) -> tuple[dict, dict]:
    """(date, ts) 두 dict. 없는 코드 = 발표일 미상.

    ts는 KST ISO8601이다 (fetch.py가 항상 +09:00로 적는다).
    """
    dates, stamps = {}, {}
    try:
        for r in conn.execute(
            "SELECT code, first_seen_date, first_seen_ts FROM disclosure WHERE ym=?",
            (ym,)
        ):
            dates[r["code"]] = r["first_seen_date"]
            stamps[r["code"]] = r["first_seen_ts"]
    except sqlite3.OperationalError:
        pass
    return dates, stamps


def load_disclosure_all(conn, codes: list[str]) -> dict:
    """code -> {ym: first_seen_date}. Sparse on purpose: only months seen since
    tracking began have an entry, so this stays small."""
    out: dict[str, dict] = {}
    try:
        rows = list(conn.execute(
            "SELECT code, ym, first_seen_date FROM disclosure "
            "WHERE first_seen_date IS NOT NULL"))
    except sqlite3.OperationalError:
        return out
    want = set(codes)
    for r in rows:
        if r["code"] in want:
            out.setdefault(r["code"], {})[r["ym"]] = r["first_seen_date"]
    return out


def load_pending_values(conn, ym: str, codes: list[str]) -> tuple[dict, dict]:
    """(rev, cum) keyed by code for the month still being filed.

    Kept out of the main record loading on purpose: rec["rev"] stops at the
    reference month so every metric in the body stays anchored there. Only the
    two pending columns and the modal's last row use these.
    """
    rev, cum = {}, {}
    lst = list(codes)
    for i in range(0, len(lst), 400):
        chunk = lst[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT code, rev_month_k, rev_cum_k FROM revenue "
            f"WHERE ym=? AND code IN ({ph})", (ym, *chunk)
        ):
            if r["rev_month_k"] is not None:
                rev[r["code"]] = r["rev_month_k"]
                cum[r["code"]] = r["rev_cum_k"]
    return rev, cum


def load_mktcap(conn) -> dict:
    """시가총액 재료. 종가나 주식수가 없으면 그 종목은 아예 빼서 빈칸이 되게 한다.

    반환: {"cap": {code: (usd_b, krw_jo)}, "date": 거래일, "fx": {...}, ...}
    """
    out = {"cap": {}, "date": None, "usdtwd": None, "usdkrw": None,
           "fx_as_of": None, "n": 0}
    try:
        fx = {r["pair"]: (r["rate"], r["as_of"])
              for r in conn.execute("SELECT pair, rate, as_of FROM fx")}
        rows = list(conn.execute(
            "SELECT c.code, c.shares, p.close_twd, p.trade_date "
            "FROM company c JOIN price p ON p.code = c.code "
            "WHERE c.shares > 0 AND p.close_twd > 0"))
    except sqlite3.OperationalError:
        return out
    if "USDTWD" not in fx or "USDKRW" not in fx:
        return out
    usdtwd, out["fx_as_of"] = fx["USDTWD"]
    usdkrw = fx["USDKRW"][0]
    out["usdtwd"], out["usdkrw"] = usdtwd, usdkrw
    if not usdtwd or not usdkrw:
        return out
    for r in rows:
        twd = r["close_twd"] * r["shares"]          # 시총 (TWD)
        usd_b = twd / usdtwd / 1e9                  # 십억 USD
        krw_jo = twd / usdtwd * usdkrw / 1e12       # 조 KRW
        out["cap"][r["code"]] = (usd_b, krw_jo)
        if out["date"] is None or (r["trade_date"] or "") > out["date"]:
            out["date"] = r["trade_date"]
    out["n"] = len(out["cap"])
    return out


def fmt_cap(v) -> str:
    """(usd_b, krw_jo) -> '$1.90T · 2,710조 원'. 없으면 빈칸.

    단위를 값 크기에 맞춰 바꾼다. 고정 단위를 쓰면 TSMC가 $1904.0B로,
    소형주가 0.5조로 찍혀 둘 다 읽기 어렵다.
    """
    if not v:
        return ""
    usd_b, krw_jo = v
    if usd_b >= 1000:
        u = f"${usd_b / 1000:,.2f}T"
    elif usd_b >= 1:
        u = f"${usd_b:,.1f}B"
    else:
        u = f"${usd_b * 1000:,.0f}M"
    if krw_jo >= 1000:
        k = f"{krw_jo:,.0f}조 원"
    elif krw_jo >= 1:
        k = f"{krw_jo:,.1f}조 원"
    else:
        k = f"{krw_jo * 10000:,.0f}억 원"
    return f'{u}<span class="capsep"> · </span><span class="capk">{k}</span>'


def load_master(conn) -> dict:
    """code -> English abbreviation. Empty when --master has never been run."""
    try:
        return {r["code"]: r["name_en"] for r in
                conn.execute("SELECT code, name_en FROM company WHERE name_en IS NOT NULL")}
    except sqlite3.OperationalError:
        return {}


def load(conn, ref_ym: str, master: dict, caps: dict | None = None
         ) -> tuple[list[dict], dict]:
    """One record per IT company, carrying its monthly and cumulative series."""
    # The IT industry filter, plus every bom_groups code regardless of its
    # official industry. 8996 高力 (액냉 CDU용 판형 열교환기) is filed under
    # 電機機械業 and would otherwise be invisible even though it sits in the
    # 쿨링 group.
    inds = list(groups.IT_INDUSTRIES)
    bom = bom_groups.all_codes()
    ph = ",".join("?" * len(inds))
    cph = ",".join("?" * len(bom))

    recs: dict[str, dict] = {}
    for r in conn.execute(
        f"SELECT code, name, industry, market, is_ky, rev_last_month_k, "
        f"rev_ly_month_k, rev_cum_k, rev_cum_ly_k, note "
        f"FROM revenue WHERE ym=? AND (industry IN ({ph}) OR code IN ({cph}))",
        (ref_ym, *inds, *bom)
    ):
        kr, layer = korean_name(r["code"], r["name"])
        ind_kr = groups.INDUSTRY_KR.get(r["industry"], r["industry"])
        recs[r["code"]] = {k: r[k] for k in r.keys()} | {
            "cap": (caps or {}).get(r["code"]),
            "name_en": master.get(r["code"]), "name_kr": kr, "name_layer": layer,
            "industry_kr": ind_kr,
            "bom_group": bom_groups.group_of(r["code"]),
            "biz": bom_groups.biz(r["code"], ind_kr),
            "biz_registered": bool((bom_groups.BIZ.get(r["code"]) or "").strip()),
            "in_it": r["industry"] in set(inds),
            "rev": {}, "cum": {}, "rev_ly": {}}

    if not recs:
        raise SystemExit(f"no IT companies at {ref_ym} -- check groups.IT_INDUSTRIES")

    # full history for exactly those companies, reference month and earlier.
    # rev_ly_month_k is carried per month so the chart's YoY line can reach back
    # past the start of our own window.
    codes = list(recs)
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        cph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT code, ym, rev_month_k, rev_cum_k, rev_ly_month_k FROM revenue "
            f"WHERE ym <= ? AND code IN ({cph})", (ref_ym, *chunk)
        ):
            rec = recs[r["code"]]
            rec["rev"][r["ym"]] = r["rev_month_k"]
            rec["cum"][r["ym"]] = r["rev_cum_k"]
            rec["rev_ly"][r["ym"]] = r["rev_ly_month_k"]

    stats = {"yoy_fallback": 0, "mom_fallback": 0, "accel_blank": 0,
             "yoy_blank": 0, "mom_blank": 0, "cum_blank": 0, "cum_no_prior": 0,
             "cum_ly_exact": 0, "cum_ly_rounding": 0, "cum_ly_differ": 0,
             "cum_ly_rows": []}
    rows = []
    for rec in recs.values():
        m = compute_metrics(rec, ref_ym, stats)
        if m["rev_now"] is None:
            continue
        rows.append(rec | m)
    rows.sort(key=lambda r: r["rev_now"], reverse=True)
    return rows, stats


def load_pending(conn, pending: list[tuple[str, int]], master: dict) -> list[dict]:
    """IT companies that have already filed for the in-progress month."""
    if not pending:
        return []
    inds = list(groups.IT_INDUSTRIES)
    bom = bom_groups.all_codes()
    ph = ",".join("?" * len(inds))
    cph = ",".join("?" * len(bom))
    out = []
    for ym, _ in pending:
        for r in conn.execute(
            f"SELECT code, name, industry, rev_month_k, rev_last_month_k, "
            f"rev_ly_month_k, ym FROM revenue "
            f"WHERE ym=? AND (industry IN ({ph}) OR code IN ({cph})) "
            f"AND rev_month_k IS NOT NULL "
            f"ORDER BY rev_month_k DESC", (ym, *inds, *bom)
        ):
            d = {k: r[k] for k in r.keys()}
            d["name_kr"] = korean_name(d["code"], d["name"])[0]
            d["name_en"] = master.get(d["code"])
            ind_kr = groups.INDUSTRY_KR.get(d["industry"], d["industry"])
            d["industry_kr"] = ind_kr
            d["bom_group"] = bom_groups.group_of(d["code"])
            d["biz"] = bom_groups.biz(d["code"], ind_kr)
            d["biz_registered"] = bool((bom_groups.BIZ.get(d["code"]) or "").strip())
            d["yoy"] = pct(d["rev_month_k"], d["rev_ly_month_k"])
            d["mom"] = pct(d["rev_month_k"], d["rev_last_month_k"])
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def mn(v_k) -> str:
    """千元 -> 백만 NTD. The only place this division happens.

    A handful of dormant companies file revenue of a few thousand 千元, which
    rounds to '0' at this scale and reads as 'no revenue'. Those get '<1' so a
    real-but-tiny figure is never mistaken for zero. Sorting is unaffected --
    the cells carry the raw 千元 value in data-v.
    """
    if v_k is None:
        return ""
    if v_k == 0:
        return "0"
    if abs(v_k) < 500:
        return "&lt;1" if v_k > 0 else "&gt;-1"
    return f"{v_k / 1000:,.0f}"


def pc(v) -> str:
    """Signed percent. Near-zero prints unsigned so we never render '-0.0'."""
    if v is None:
        return ""
    if abs(v) < FLAT_EPS:
        return "0.0"
    return f"{v:+,.1f}"


FLAT_EPS = 0.05     # |값| < 이 값이면 '변화 없음'으로 보고 회색


def cls(v) -> str:
    """한국식: 플러스 빨강, 마이너스 파랑, 0 근처는 회색."""
    if v is None:
        return "na"
    if abs(v) < FLAT_EPS:
        return "flat"
    return "up" if v > 0 else "dn"


def esc(s) -> str:
    return html.escape(str(s)) if s is not None else ""


def sort_key(v):
    """Value a JS sort can use; empty string keeps blanks at the bottom."""
    return "" if v is None else f"{v:.6f}"


def korean_name(code: str, zh: str | None) -> tuple[str, str]:
    """(displayed name, which layer supplied it).

    NAME_KR is hand-curated and wins. An empty string there means 'not decided
    yet', not 'blank name', so it falls through to the mechanical reading.
    """
    manual = (NAME_KR.get(code) or "").strip()
    if manual:
        return manual, "manual"
    auto = (NAME_AUTO.get(code) or "").strip()
    if auto:
        return auto, "auto"
    return (zh or code), "zh"


def name_cell(rec, with_biz: bool = False) -> str:
    """한글명 + 회색 영문약칭.

    한자명은 title 속성으로만 노출한다. with_biz면 사업설명까지 붙여
    "한자명 — 사업설명"이 된다. 전체 종목 표는 컬럼을 늘리지 않고 이걸 쓴다.
    """
    kr = esc(rec["name_kr"])
    en = rec.get("name_en")
    bits = [rec.get("name") or ""]
    # Only a *registered* description goes in the tooltip. The
    # "(설명 미등록)" fallback is useful in a dedicated column where it reads as
    # a to-do, but as a tooltip on 800+ non-BoM rows it is pure noise.
    if with_biz and rec.get("biz_registered"):
        bits.append(rec["biz"])
    tip = " — ".join(b for b in bits if b)
    tip_attr = f' title="{esc(tip)}"' if tip else ""
    en_html = f' <span class="en">{esc(en)}</span>' if en else ""
    return f'<span class="nm"{tip_attr}>{kr}</span>{en_html}'


def name_plain(rec) -> str:
    """Everything the search box should match: 한글 / 영문 / 한자."""
    bits = [rec.get("name_kr") or "", rec.get("name") or "", rec.get("name_en") or ""]
    return " ".join(b for b in bits if b)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
CSS = f"""
:root {{
  --bg:{BG}; --fg:{FG}; --a1:{ACCENT}; --a2:{ACCENT2}; --a3:{ACCENT3};
  --panel:#161d24; --line:#243039; --mute:#93a4b1;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:32px 28px 80px;
  background:var(--bg); color:var(--fg);
  font-family:'42dot Sans','Microsoft JhengHei','Noto Sans TC',
              'Malgun Gothic',sans-serif;
  font-size:20px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}}
.topline {{
  font-size:19px; color:var(--mute); margin:0 0 10px; padding:9px 16px;
  background:var(--panel); border:1px solid var(--line); border-radius:8px;
  border-left:5px solid var(--a1); display:inline-block;
}}
.topline b {{ color:var(--fg); }}
h1 {{ font-size:38px; font-weight:800; margin:0 0 6px; letter-spacing:-.5px; }}
.cbmark {{
  height:1.1em; width:auto; margin-left:.28em; vertical-align:-0.18em;
  border-radius:6px;
}}
.cbmark-e {{ margin-left:.28em; font-size:.92em; vertical-align:-0.02em; }}
h2 {{
  font-size:26px; font-weight:700; margin:52px 0 14px;
  padding-left:14px; border-left:6px solid var(--a3);
}}
h2 .n {{ color:var(--a3); margin-right:10px; }}
.sub {{ color:var(--mute); font-size:20px; margin:0 0 4px; }}
.meta {{ color:var(--mute); font-size:18px; }}
.wrap {{ max-width:1680px; margin:0 auto; }}

.cards {{ display:flex; flex-wrap:wrap; gap:16px; margin:18px 0 8px; }}
.card {{
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:16px 22px; min-width:190px;
}}
.card .k {{ color:var(--mute); font-size:18px; }}
.card .v {{ font-size:30px; font-weight:800; color:var(--a1); }}
.card .v.sm {{ font-size:24px; }}

.note {{
  background:#1a2129; border:1px solid var(--line); border-left:5px solid var(--a3);
  border-radius:8px; padding:14px 20px; margin:14px 0; color:#c9d6e0; font-size:19px;
}}

.tools {{
  display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:14px 0;
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px;
}}
select {{
  background:#0b1015; color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:11px 13px; font-size:19px; font-family:inherit;
  max-width:230px;
}}
select:focus {{ outline:2px solid var(--a3); }}
.chk {{
  display:inline-flex; align-items:center; gap:8px; font-size:19px;
  cursor:pointer; user-select:none; white-space:nowrap;
}}
.chk input {{ width:20px; height:20px; accent-color:var(--a3); cursor:pointer; }}
.count {{ margin-left:auto; }}
.count b {{ color:var(--a1); font-size:22px; }}
input[type=search] {{
  background:#0b1015; color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:11px 16px; font-size:20px;
  /* a flat min-width forces the page wider than a narrow viewport */
  width:min(420px,100%); font-family:inherit;
}}
input[type=search]:focus {{ outline:2px solid var(--a1); border-color:var(--a1); }}
.count {{ color:var(--mute); font-size:19px; }}

/* 600px cap + internal scroll. The sticky header anchors to this container,
   so the column names stay put while the rows move. */
.scroll {{
  max-height:600px; overflow:auto;
  border:1px solid var(--line); border-radius:10px;
}}
table {{ border-collapse:separate; border-spacing:0; width:100%;
        font-variant-numeric:tabular-nums; }}
thead th {{
  position:sticky; top:0; z-index:2;
  background:#1b232b; color:#dce7ef; font-size:19px; font-weight:700;
  text-align:right; padding:13px 14px; white-space:nowrap;
  border-bottom:2px solid var(--line); cursor:pointer; user-select:none;
}}
thead th.l {{ text-align:left; }}
thead th:hover {{ color:var(--a3); }}
thead th .ar {{ opacity:.35; font-size:15px; margin-left:5px; }}
thead th.asc .ar, thead th.desc .ar {{ opacity:1; color:var(--a3); }}
tbody td {{
  padding:11px 14px; text-align:right; white-space:nowrap;
  border-bottom:1px solid #1c252d; font-size:20px;
}}
tbody td.l {{ text-align:left; }}
/* rows need an explicit background or the sticky code column shows through */
tbody tr {{ background:{BG}; }}
tbody tr:nth-child(even) {{ background:#12191f; }}
tbody tr:hover {{ background:#1d2833; }}
/* code column pinned left, so a horizontally scrolled row stays identifiable */
thead th.pin {{ left:0; z-index:3; text-align:left; }}
tbody td.pin {{ position:sticky; left:0; z-index:1; background:inherit; }}
.code {{ color:#8fb8dc; font-weight:700; }}
.ind {{ color:var(--mute); font-size:18px; }}
.bom {{ color:var(--a1); font-size:17px; font-weight:600; }}
.up {{ color:var(--a3); font-weight:700; }}      /* 플러스 = 빨강(따뜻한 쪽) */
.dn {{ color:var(--a2); font-weight:700; }}      /* 마이너스 = 파랑 */
.flat {{ color:var(--mute); }}                    /* 0 근처 */
.na {{ color:#55636e; }}
.rev {{ font-weight:700; }}

/* min() inside minmax keeps auto-fit from demanding 400px on narrow screens */
.movers {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(min(400px,100%),1fr)); gap:20px; }}
/* movers tables need their own scroll container too, or they push the page wide */
.mv {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
      padding:6px 4px 4px; overflow-x:auto; }}
.mv h3 {{ font-size:21px; margin:12px 16px 10px; font-weight:700; }}
.mv h3 .tag {{ font-size:17px; color:var(--mute); font-weight:400; margin-left:8px; }}
.mv table {{ font-size:19px; }}
.mv tbody td {{ font-size:19px; padding:9px 14px; vertical-align:top; }}
.mv .scroll, .mv table {{ max-height:none; }}
.mvbiz {{ color:var(--mute); font-size:16px; font-weight:400; margin-top:2px;
         white-space:normal; max-width:270px; line-height:1.35; }}
td.biz {{ color:#c9d6e0; font-size:17px; white-space:normal; min-width:280px; }}
.en {{ color:var(--mute); font-weight:400; font-size:17px; }}
/* dotted underline hints that hovering reveals the Chinese name */
.nm {{ border-bottom:1px dotted #35424d; cursor:help; }}

/* --- section 4 charts --- */
.grid3 {{
  display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:20px; margin-top:16px;
}}
@media (max-width:1200px) {{ .grid3 {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
@media (max-width:760px)  {{ .grid3 {{ grid-template-columns:minmax(0,1fr); }} }}
.cbox {{
  margin:0; background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:14px 12px 10px; min-width:0;
}}
.chd {{ font-size:22px; font-weight:700; margin:0 2px 8px; line-height:1.3; }}
.chd .code {{ color:var(--a2); }}
.chd .en {{ font-size:18px; }}
.cind {{ font-size:16px; font-weight:400; color:var(--mute); margin-top:2px; }}
svg.ch {{ display:block; width:100%; height:auto; background:var(--bg); border-radius:6px; }}
svg.ch text {{ font-family:inherit; fill:var(--fg); }}
svg.ch .cl {{ font-size:22px; font-weight:700; }}      /* 22px bold, as specified */
svg.ch .cmax {{ fill:var(--a2); }}
/* MOM_COL / LINE_COL are the same palette entries, spelled with the names that
   exist before this f-string is evaluated */
svg.ch .cmom {{ fill:{ACCENT}; }}    /* 헤더 숫자를 선 색과 맞춘다 */
svg.ch .cyoy {{ fill:{ACCENT3}; }}
body.hide-mom svg.ch .momline, body.hide-mom svg.ch .momdot {{ display:none; }}
.momtog {{ font-size:18px; margin-bottom:6px; }}
.hintw {{ font-size:16px; color:var(--mute); }}

/* month labels -- grid stays three columns whether they are on or off */
svg.ch .lbl {{ display:none; }}
body.show-nums svg.ch .lbl {{ display:block; }}
body.show-nums.hide-mom svg.ch .lbl.momlbl {{ display:none; }}
svg.ch .lbg {{ fill:rgba(10,15,20,.82); rx:1.2; }}
/* 색은 반드시 CSS로 준다. SVG의 fill 표현 속성은 `svg.ch text {{fill:...}}`
   보다 우선순위가 낮아, JS로 fill 속성을 걸면 전부 흰색으로 덮인다. */
svg.ch .lbt {{
  font-size:{LBL_FS}px; font-weight:700; text-anchor:middle;
  letter-spacing:0; fill:{ACCENT3};
}}
svg.ch .lbl.momlbl .lbt {{ fill:{ACCENT}; }}
/* clickable chart card */
.cbox {{ cursor:pointer; transition:border-color .12s, transform .12s; }}
.cbox:hover {{ border-color:var(--a1); transform:translateY(-2px); }}
table.clickable tbody tr {{ cursor:pointer; }}

/* --- monthly detail modal --- */
.mdback {{
  position:fixed; inset:0; z-index:50; background:rgba(4,8,12,.78);
  display:flex; align-items:center; justify-content:center; padding:24px;
}}
.mdback[hidden] {{ display:none; }}
.mdbox {{
  background:var(--panel); border:1px solid var(--line); border-radius:14px;
  width:min(920px,100%); max-height:92vh; display:flex; flex-direction:column;
  box-shadow:0 24px 60px rgba(0,0,0,.6);
}}
.mdhead {{ padding:18px 22px 12px; border-bottom:1px solid var(--line); }}
.mdtitle {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:10px; }}
.mdcode {{ color:#8fb8dc; font-weight:800; font-size:26px; }}
.mdkr {{ font-size:26px; font-weight:800; }}
.mden {{ font-size:19px; color:var(--mute); }}
.mdzh {{ font-size:19px; color:var(--mute); }}
.mdtags {{ margin-top:7px; display:flex; flex-wrap:wrap; gap:8px; }}
.mdtags span {{
  font-size:16px; padding:3px 10px; border-radius:20px;
  border:1px solid var(--line); color:var(--mute);
}}
.mdtags .mdgrp {{ color:var(--a1); border-color:#3d3a20; }}
.mdbiz {{ margin-top:9px; font-size:18px; color:#c9d6e0; }}
.mddisc {{
  padding:9px 22px; font-size:18px; border-bottom:1px solid var(--line);
}}
.mddisc.ok {{ color:var(--a1); }}
.mddisc.pend {{ color:var(--a3); font-weight:700; }}
.mddisc.none {{ color:var(--mute); }}
table.mdtab tr.pendrow td {{ background:#1a2028; border-top:2px solid var(--a3); }}
.unfiled {{ color:#6d7a85; font-style:italic; }}
td.unfiled {{ color:#6d7a85; font-style:italic; font-weight:400; }}
.mdbar {{
  display:flex; align-items:center; gap:10px; padding:10px 22px;
  border-bottom:1px solid var(--line);
}}
.mdnav, .mdcsv, .mdx {{
  background:#0b1015; color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:8px 14px; font-size:18px; font-family:inherit;
  cursor:pointer;
}}
.mdnav:hover, .mdcsv:hover, .mdx:hover {{ border-color:var(--a1); color:var(--a1); }}
.mdpos {{ font-size:17px; color:var(--mute); min-width:64px; text-align:center; }}
.mdmsg {{ font-size:17px; color:var(--a1); }}
.mdx {{ margin-left:auto; }}
.mdscroll {{ max-height:500px; border:0; border-radius:0; margin:0; }}
table.mdtab {{ width:100%; }}
table.mdtab thead th {{ background:#1b232b; cursor:default; }}
table.mdtab tbody td {{ font-size:19px; padding:9px 16px; }}
svg.ch .csep {{ fill:var(--mute); font-weight:400; }}
svg.ch .cs {{ font-size:15px; fill:var(--mute); font-weight:400; }}
svg.ch .ct {{ font-size:16px; fill:var(--mute); }}
.cbiz {{
  min-height:30px; margin:8px 2px 0; padding-top:8px;
  border-top:1px dashed var(--line); color:#c9d6e0; font-size:17px; line-height:1.4;
}}
.ghd {{
  font-size:24px; font-weight:800; margin:38px 0 4px; padding:8px 0 8px 14px;
  border-left:6px solid var(--a1); background:linear-gradient(90deg,#1a222a,transparent);
  display:flex; flex-wrap:wrap; align-items:baseline; gap:12px;
}}
.ghd .gn {{ font-size:19px; font-weight:600; color:var(--a1); }}
.ghd .gsum {{ font-size:18px; font-weight:400; color:var(--mute); margin-left:auto; }}
svg.ch rect[fill="{ACCENT2}"] {{ cursor:crosshair; }}
.chk.off {{ opacity:.4; }}

/* --- 발표 타임라인 --- */
.tlhead {{
  display:flex; flex-wrap:wrap; align-items:center; gap:18px; margin:14px 0;
  background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:16px 20px;
}}
.tlbig {{ display:flex; flex-direction:column; line-height:1.15; }}
.tlbig b {{ font-size:38px; font-weight:800; color:var(--a1); }}
.tlbig span {{ font-size:16px; color:var(--mute); }}
.tlbig.dim b {{ color:var(--mute); }}
.tlbig.ok b {{ font-size:26px; color:var(--fg); }}
.tlbig.warn b {{ font-size:26px; color:var(--a3); }}
.tlmeta {{ font-size:17px; color:var(--mute); line-height:1.5; }}
.tlmeta b {{ color:var(--fg); }}
.tlchk {{ margin-left:auto; display:flex; flex-wrap:wrap; gap:14px; }}
td.cap {{ color:#cfe0ec; font-size:18px; white-space:nowrap; }}
td.cap .capk {{ color:var(--mute); font-size:16px; }}
.capsep {{ color:#4d5b67; }}
.ccap {{ margin-left:10px; color:#a9bccb; }}
.ccap .capk {{ color:var(--mute); }}
td.tlts {{ color:var(--a1); font-weight:700; font-variant-numeric:tabular-nums; }}
/* 부품군 종목은 배경으로 구분한다 -- 이 행들이 실제로 보는 대상이다 */
table tbody tr.isbom {{ background:#16212b; }}
table tbody tr.isbom:nth-child(even) {{ background:#18242f; }}
table tbody tr.isbom td.pin {{ background:inherit; }}
table tbody tr.isbom:hover {{ background:#1f2e3a; }}
details.unf {{
  margin-top:14px; background:var(--panel); border:1px solid var(--line);
  border-radius:10px; padding:12px 16px;
}}
details.unf summary {{ cursor:pointer; font-size:19px; color:#c9d6e0; }}
details.unf summary b {{ color:var(--a3); }}
details.unf .scroll {{ margin-top:12px; max-height:420px; }}

/* --- section 4 heatmap --- */
.hscroll {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; }}
table.hm {{ border-collapse:separate; border-spacing:0; font-variant-numeric:tabular-nums; }}
table.hm th.hl {{
  position:sticky; left:0; z-index:3; background:#1b232b;
  min-width:250px; max-width:250px; text-align:left; padding:9px 12px;
  font-size:19px; font-weight:700; cursor:default; border-bottom:1px solid var(--line);
}}
table.hm thead th.hl {{ z-index:4; }}
table.hm .hsub {{ display:block; font-size:15px; font-weight:400; color:var(--mute); }}
table.hm th.hmth {{
  position:sticky; top:0; z-index:2; background:#1b232b; color:#dce7ef;
  font-size:15px; font-weight:600; padding:8px 4px; min-width:42px;
  text-align:center; cursor:default; border-bottom:2px solid var(--line);
}}
/* the newest column is the complete month everything else is anchored to */
table.hm th.hmth.refm {{
  background:#2b3742; color:var(--a1); font-size:17px; font-weight:800;
  border-left:2px solid var(--a1);
}}
table.hm th.hmth.refm .rtag {{
  display:block; font-size:12px; font-weight:600; color:var(--a1); letter-spacing:0;
}}
table.hm td.hc.sat {{ font-weight:800; }}
.satlegend {{ color:#fff; font-weight:800; }}
table.hm td.hc {{
  position:relative; text-align:center; padding:9px 4px; min-width:42px;
  font-size:17px; font-weight:700; border-right:1px solid #0f1419;
  border-bottom:1px solid #0f1419;
}}
/* partial like-for-like sum */
table.hm td.hc.part::after {{
  content:""; position:absolute; bottom:2px; left:50%; transform:translateX(-50%);
  width:3px; height:3px; border-radius:50%; background:rgba(255,255,255,.75);
}}
table.hm tr.stg td {{
  background:#1a2129; color:var(--a1); font-size:17px; font-weight:700;
  padding:7px 12px; letter-spacing:1px;
  position:sticky; left:0;
}}
table.hm tr.bench th.hl {{ background:#233040; color:var(--a1); }}
table.hm tr.tiny th.hl {{ background:#151b21; color:#8b9aa6; }}
.note.fn {{ border-left-color:var(--mute); font-size:18px; }}

/* ⓘ tooltip: keeps the legend available without a box above the grid */
.info {{
  position:relative; display:inline-block; margin-left:10px; font-size:22px;
  color:var(--a1); cursor:help; vertical-align:middle; font-weight:400;
}}
.info .tip {{
  display:none; position:absolute; left:0; top:135%; z-index:30;
  width:min(640px,84vw); background:#1b232b; border:1px solid var(--line);
  border-radius:10px; padding:15px 19px; font-size:18px; font-weight:400;
  color:#dbe6ee; line-height:1.55; box-shadow:0 14px 36px rgba(0,0,0,.55);
  white-space:normal; letter-spacing:0;
}}
.info:hover .tip, .info:focus .tip {{ display:block; }}
.thinbar {{ margin:12px 0 4px; }}
.thinbar .chk {{ font-size:18px; color:var(--mute); }}

/* heatmap YoY / MoM tabs */
.tabs {{ display:inline-flex; gap:0; margin-left:14px; vertical-align:middle; }}
.tab {{
  background:#0b1015; color:var(--mute); border:1px solid var(--line);
  padding:7px 20px; font-size:19px; font-weight:700; font-family:inherit;
  cursor:pointer; letter-spacing:0;
}}
.tab:first-child {{ border-radius:8px 0 0 8px; }}
.tab:not(:first-child) {{ border-left:0; }}
.tab:last-child {{ border-radius:0 8px 8px 0; }}
.tab:hover {{ color:var(--fg); }}
.tab.on {{ background:var(--a3); color:#10161c; border-color:var(--a3); }}

/* --- section 5 inflection --- */
.ihd {{
  font-size:23px; font-weight:800; margin:34px 0 10px;
  display:flex; flex-wrap:wrap; align-items:baseline; gap:12px;
}}
.ihd .gn {{ font-size:19px; font-weight:600; color:var(--a1); }}
.ihd .inote {{ font-size:17px; font-weight:400; color:var(--mute); }}
.none {{
  background:var(--panel); border:1px dashed var(--line); border-radius:10px;
  padding:18px 22px; color:var(--mute); font-size:19px;
}}
table.itab tbody td {{ font-size:19px; }}
.peak {{ color:var(--a1); font-weight:700; }}
.hshort {{ color:var(--a3); font-weight:700; }}
.hide {{ display:none !important; }}
footer {{ margin-top:60px; color:var(--mute); font-size:17px; border-top:1px solid var(--line); padding-top:18px; }}
"""

JS = """
function tsort(tbl, idx, th){
  var tb = tbl.tBodies[0];
  var rows = Array.prototype.slice.call(tb.rows);
  var dir = th.classList.contains('asc') ? -1 : 1;
  Array.prototype.forEach.call(th.parentNode.cells, function(c){
    c.classList.remove('asc','desc');
  });
  th.classList.add(dir === 1 ? 'asc' : 'desc');
  rows.sort(function(a, b){
    var x = a.cells[idx].dataset.v, y = b.cells[idx].dataset.v;
    var bx = (x === '' || x == null), by = (y === '' || y == null);
    if (bx && by) return 0;
    if (bx) return 1;              // blanks always sink
    if (by) return -1;
    var nx = parseFloat(x), ny = parseFloat(y);
    if (!isNaN(nx) && !isNaN(ny)) return (nx - ny) * dir;
    return String(x).localeCompare(String(y)) * dir;
  });
  rows.forEach(function(r){ tb.appendChild(r); });
}
function wire(tblId){
  var t = document.getElementById(tblId);
  if (!t) return;
  Array.prototype.forEach.call(t.tHead.rows[0].cells, function(th, i){
    th.addEventListener('click', function(){ tsort(t, i, th); });
  });
}
// Search + sector + market + BoM group + two checkboxes, all ANDed together.
// The count reflects whatever combination is active, including the
// small-revenue filter that starts switched on.
function wireFilters(cfg){
  var t = document.getElementById(cfg.table);
  if (!t) return;
  var rows = Array.prototype.slice.call(t.tBodies[0].rows);
  var total = rows.length;
  var el = {};
  ['q','ind','mkt','bom','small','bomOnly','unfiled','cap','count'].forEach(function(k){
    el[k] = cfg[k] ? document.getElementById(cfg[k]) : null;
  });
  function run(){
    var q = el.q ? el.q.value.trim().toLowerCase() : '';
    var ind = el.ind ? el.ind.value : '';
    var mkt = el.mkt ? el.mkt.value : '';
    var bom = el.bom ? el.bom.value : '';
    var unfiled = el.unfiled ? el.unfiled.checked : false;
    // 시총 필터는 이 표에서만 쓴다. 차트/히트맵/변곡점검/movers는 손대지 않는다.
    var capMin = (el.cap && el.cap.checked) ? cfg.capFloorJo : 0;
    var bomOnly = el.bomOnly ? el.bomOnly.checked : false;
    // The 113 BoM stocks are hand-picked, so the small-revenue cutoff has no
    // job there -- it would silently drop 3234 光環 (77 백만). Neutralise it and
    // dim the control so it is obvious why it stopped applying.
    var hideSmall = (el.small && !bomOnly) ? el.small.checked : false;
    if (el.small){
      el.small.disabled = bomOnly;
      var lab = el.small.closest('.chk');
      if (lab) lab.classList.toggle('off', bomOnly);
    }
    var n = 0;
    rows.forEach(function(r){
      var d = r.dataset;
      var ok = (q === '' || d.s.indexOf(q) !== -1)
            && (ind === '' || d.ind === ind)
            && (mkt === '' || d.mkt === mkt)
            && (bom === '' || d.bom === bom)
            && (!bomOnly || d.bom !== '')
            && (!unfiled || d.filed === '0')
            && (!capMin || parseFloat(d.cap || 0) >= capMin)
            && (!hideSmall || parseFloat(d.rev) >= cfg.floor);
      r.classList.toggle('hide', !ok);
      if (ok) n++;
    });
    if (el.count){
      el.count.innerHTML = (n === total)
        ? '<b>' + total + '</b>개 종목'
        : '<b>' + n + '</b> / ' + total + '개 종목';
    }
  }
  ['q','ind','mkt','bom','small','bomOnly','unfiled','cap'].forEach(function(k){
    if (el[k]) el[k].addEventListener(k === 'q' ? 'input' : 'change', run);
  });
  run();
}
// mark a column as the active sort without reordering (rows arrive pre-sorted)
function markSorted(tblId, idx, dir){
  var t = document.getElementById(tblId);
  if (!t) return;
  t.tHead.rows[0].cells[idx].classList.add(dir);
}
"""


def th(label, cls_="", hint=""):
    t = f' title="{esc(hint)}"' if hint else ""
    c = f' class="{cls_}"' if cls_ else ""
    return f'<th{c}{t}>{esc(label)}<span class="ar">&#9650;&#9660;</span></th>'


def render_pending(pending, prows, ref_ym, it_total, n=1):
    if not pending:
        return (f'<h2><span class="n">{n}</span>조기공시분</h2>'
                '<div class="note">현재 진행 중인 공시월이 없습니다. '
                f'최신 완결월 {esc(ref_ym)} 기준입니다.</div>')
    ym, all_n = pending[0]
    filed = len(prows)
    dl, rolled = filing_deadline(ym)
    parts = [f'<h2><span class="n">{n}</span>조기공시분 &mdash; {esc(ym)} '
             f'<span class="meta">IT {filed} / {it_total} 발표 '
             f'({filed / it_total * 100:.1f}%) · 마감 {dl[5:7]}/{dl[8:]}</span></h2>',
             '<div class="note">이 달은 아직 신고 기간입니다. 대만 규정은 익월 10일까지이고 '
             f'주말이면 순연되어 이번 마감은 <b>{esc(dl)}</b>'
             + ('(10일이 주말이라 순연)' if rolled else '')
             + ' 입니다 &mdash; <b>대만 공휴일 순연은 미반영</b>입니다.'
             f'<br>다른 모든 섹션은 완결월 <b>{esc(ref_ym)}</b> 기준이며 '
             '이 수치는 어디에도 섞이지 않습니다.</div>',
             '<div class="cards">',
             f'<div class="card"><div class="k">IT 발표 종목</div>'
             f'<div class="v">{filed}<span style="font-size:20px;color:var(--mute)"> / {it_total}</span></div></div>',
             f'<div class="card"><div class="k">전체 시장 발표</div>'
             f'<div class="v">{all_n}</div></div>',
             f'<div class="card"><div class="k">IT 공시율</div>'
             f'<div class="v">{filed / it_total * 100:.1f}%</div></div>',
             '</div>']
    if prows:
        parts.append('<div class="scroll"><table id="tPend"><thead><tr>')
        parts.append(th("코드", "l pin") + th("종목명", "l") + th("업종", "l")
                     + th("부품군", "l")
                     + th("당월매출", "", "백만 NTD") + th("MoM%") + th("YoY%")
                     + th("사업", "l"))
        parts.append("</tr></thead><tbody>")
        for r in prows:
            g = r["bom_group"] or ""
            parts.append(
                f'<tr>'
                f'<td class="l pin code" data-v="{esc(r["code"])}">{esc(r["code"])}</td>'
                f'<td class="l" data-v="{esc(r["name_kr"])}">{name_cell(r)}</td>'
                f'<td class="l ind" data-v="{esc(r["industry_kr"])}" '
                f'title="{esc(r["industry"])}">{esc(r["industry_kr"])}</td>'
                f'<td class="l bom" data-v="{esc(g)}">{esc(g)}</td>'
                f'<td class="rev" data-v="{r["rev_month_k"]}">{mn(r["rev_month_k"])}</td>'
                f'<td class="{cls(r["mom"])}" data-v="{sort_key(r["mom"])}">{pc(r["mom"])}</td>'
                f'<td class="{cls(r["yoy"])}" data-v="{sort_key(r["yoy"])}">{pc(r["yoy"])}</td>'
                f'<td class="l biz" data-v="{esc(r["biz"])}">{esc(r["biz"])}</td>'
                f'</tr>')
        parts.append("</tbody></table></div>")
    return "\n".join(parts)


def render_timeline(rows, pend, ref_ym, mc=None, sec=1):
    """진행 중인 달의 발표 타임라인. 최신 감지순.

    대시보드를 열자마자 '지금 누가 발표했고 누가 아직인지'가 보이도록 맨 위에 둔다.
    """
    if not pend:
        return ""
    mc = mc or {"cap": {}}
    ym, stamps, dates = pend["ym"], pend["stamps"], pend["dates"]
    vals, dl = pend["vals"], pend["deadline"]
    lab = f"{ym[2:4]}/{ym[5:7]}"
    days_left = (date.fromisoformat(dl) - date.today()).days

    by_code = {r["code"]: r for r in rows}
    filed, unfiled = [], []
    for r in rows:
        v = vals.get(r["code"])
        (filed if v is not None else unfiled).append(r)

    def ts_key(r):
        # 발표시각을 모르는 건(추적 이전 적재분) 맨 아래로
        return stamps.get(r["code"]) or ""
    filed.sort(key=lambda r: (ts_key(r), r["rev_now"] or 0), reverse=True)
    unfiled.sort(key=lambda r: r["rev_now"] or 0, reverse=True)

    newest = max((s for s in stamps.values() if s), default=None)
    newest_txt = f"{newest[5:10]} {newest[11:16]} KST" if newest else "아직 없음"
    n_bom_filed = sum(1 for r in filed if r["bom_group"])

    if days_left > 1:
        dtxt, dcls = f"마감까지 {days_left}일", "ok"
    elif days_left == 1:
        dtxt, dcls = "마감 내일", "warn"
    elif days_left == 0:
        dtxt, dcls = "오늘 마감", "warn"
    else:
        dtxt, dcls = f"마감 {-days_left}일 지남", "warn"

    parts = [
        f'<h2><span class="n">{sec}</span>발표 타임라인 &mdash; {esc(ym)}</h2>',
        '<div class="tlhead">'
        f'<div class="tlbig"><b id="tlNFiled">{len(filed)}</b><span>발표</span></div>'
        f'<div class="tlbig dim"><b id="tlNUnfiled">{len(unfiled)}</b>'
        '<span>미발표</span></div>'
        f'<div class="tlbig {dcls}"><b>{esc(dtxt)}</b>'
        f'<span>{esc(dl)}</span></div>'
        f'<div class="tlmeta">부품군 <b id="tlNBom">{n_bom_filed}</b> / '
        f'{sum(1 for r in rows if r["bom_group"])} 발표'
        f'<br>마지막 감지 <b>{esc(newest_txt)}</b></div>'
        '<div class="tlchk">'
        '<label class="chk"><input type="checkbox" id="tlBom">부품군만</label>'
        + ('<label class="chk"><input type="checkbox" id="tlCap">'
           '시총 1조원 이상만</label>' if mc.get("cap") else "")
        + '</div></div>',
        '<div class="scroll"><table id="tTL"><thead><tr>'
        + th("발표시각(KST)", "l") + th("코드", "l pin") + th("한글명", "l")
        + th("부품군", "l") + th("시가총액", "l", "종가 x 보통주 발행주식수")
        + th("당월매출", "", "백만 NTD")
        + th("MoM%") + th("YoY%")
        + '</tr></thead><tbody>']

    for r in filed:
        g = r["bom_group"] or ""
        ts = stamps.get(r["code"])
        shown = f"{ts[5:10]} {ts[11:16]}" if ts else "&mdash;"
        mom = pct(vals[r["code"]], r["rev"].get(shift_ym(ym, -1)))
        yoy = pct(vals[r["code"]], r["rev"].get(shift_ym(ym, -12)))
        parts.append(
            f'<tr data-bom="{esc(g)}" '
            f'data-cap="{(r["cap"] or [0, 0])[1]:.4f}"'
            f'{" class=isbom" if g else ""}>'
            f'<td class="l tlts" data-v="{esc(ts or "")}">{shown}</td>'
            f'<td class="l pin code" data-v="{esc(r["code"])}">{esc(r["code"])}</td>'
            f'<td class="l" data-v="{esc(r["name_kr"])}">{name_cell(r)}</td>'
            f'<td class="l bom" data-v="{esc(g)}">{esc(g)}</td>'
            f'<td class="l cap" data-v="{(r["cap"] or [0])[0]:.4f}">'
            f'{fmt_cap(r["cap"])}</td>'
            f'<td class="rev" data-v="{vals[r["code"]]}">{mn(vals[r["code"]])}</td>'
            f'<td class="{cls(mom)}" data-v="{sort_key(mom)}">{pc(mom)}</td>'
            f'<td class="{cls(yoy)}" data-v="{sort_key(yoy)}">{pc(yoy)}</td>'
            f'</tr>')
    parts.append("</tbody></table></div>")

    # 아직 안 낸 곳은 접어둔다. 마감이 다가올수록 이 목록이 본론이 된다.
    parts.append(
        '<details class="unf"><summary>아직 발표하지 않은 '
        f'<b id="tlUnfShown">{len(unfiled)}</b>종목 '
        f'(부품군 {sum(1 for r in unfiled if r["bom_group"])}종목 포함)</summary>'
        '<div class="scroll"><table id="tTLU"><thead><tr>'
        + th("코드", "l") + th("한글명", "l") + th("부품군", "l")
        + (th("시가총액", "l") if mc.get("cap") else "")
        + th(f"{esc(ref_ym)} 매출", "", "완결월 기준 규모")
        + '</tr></thead><tbody>')
    for r in unfiled:
        g = r["bom_group"] or ""
        parts.append(
            f'<tr data-bom="{esc(g)}" '
            f'data-cap="{(r["cap"] or [0, 0])[1]:.4f}"'
            f'{" class=isbom" if g else ""}>'
            f'<td class="l code" data-v="{esc(r["code"])}">{esc(r["code"])}</td>'
            f'<td class="l" data-v="{esc(r["name_kr"])}">{name_cell(r)}</td>'
            f'<td class="l bom" data-v="{esc(g)}">{esc(g)}</td>'
            + (f'<td class="l cap" data-v="{(r["cap"] or [0])[0]:.4f}">'
               f'{fmt_cap(r["cap"])}</td>' if mc.get("cap") else "")
            + f'<td class="rev" data-v="{r["rev_now"]}">{mn(r["rev_now"])}</td></tr>')
    parts.append("</tbody></table></div></details>")

    parts.append(
        '<div class="note fn">'
        '· <b>발표시각은 이 시스템이 처음 감지한 시각이며, 회사의 실제 신고 '
        '시각이 아닙니다.</b> 폴링 주기(공시기간 1시간, 평시 6시간)만큼 늦게 '
        '잡힐 수 있습니다'
        '<br>· 추적은 2026-08부터 시작해 그 이전 데이터는 <b>&mdash;</b>로 '
        '표시됩니다'
        '<br>· 시각은 한국시간(KST)입니다. 대만은 1시간 느립니다'
        + (f'<br>· 시가총액 = 종가 x 보통주 발행주식수. <b>종가 기준일 '
           f'{esc(mc.get("date") or "?")}</b> (대만장 마감은 대만시간 13:30이라 '
           f'장중 실행이면 전일 종가입니다). 환율 기준 '
           f'{esc((mc.get("fx_as_of") or "?"))}'
           '<br>· <b>우선주가 있는 회사는 보통주만 계산되어 실제보다 작게 '
           '나옵니다.</b> 종가나 주식수가 없는 종목은 빈칸입니다'
           if mc.get("cap") else "")
        + '</div>')
    return "\n".join(parts)


def opts(values, label):
    o = [f'<option value="">{esc(label)}</option>']
    for v in values:
        o.append(f'<option value="{esc(v)}">{esc(v)}</option>')
    return "".join(o)


def render_table(rows, ref_ym, n=2, clickable=False, pend=None, mc=None):
    mc = mc or {"cap": {}}
    has_cap = bool(mc.get("cap"))
    floor = groups.MIN_REV_FOR_MOVERS_K
    p_ym = pend["ym"] if pend else None
    p_lab = f"{p_ym[2:4]}/{p_ym[5:7]}" if p_ym else ""
    inds = sorted({r["industry_kr"] for r in rows})
    mkts = sorted({("TWSE" if r["market"] == "TWSE" else "TPEx") for r in rows})
    boms = [g for g in bom_groups.GROUPS if any(r["bom_group"] == g for r in rows)]
    n_bom = sum(1 for r in rows if r["bom_group"])

    parts = [f'<h2><span class="n">{n}</span>종목 표 &mdash; {esc(ref_ym)}</h2>',
             '<div class="tools">'
             '<input type="search" id="q" '
             'placeholder="한글·영문·한자·코드 검색 &mdash; 파이슨 / Phison / 群聯 / 8299" '
             'autocomplete="off">'
             f'<select id="fInd">{opts(inds, "전체 섹터")}</select>'
             f'<select id="fMkt">{opts(mkts, "전체 시장")}</select>'
             f'<select id="fBom">{opts(boms, "전체 부품군")}</select>'
             '<label class="chk"><input type="checkbox" id="fSmall" checked>'
             f'{floor // 1000:,}백만 미만 숨기기</label>'
             '<label class="chk"><input type="checkbox" id="fBomOnly">'
             f'부품군 종목만 ({n_bom})</label>'
             + ('<label class="chk"><input type="checkbox" id="fUnfiled">'
                f'{p_lab} 미발표만</label>' if p_ym else "")
             # 시총 필터는 이 표에서만, 기본 꺼짐. 다른 섹션은 건드리지 않는다.
             + ('<label class="chk"><input type="checkbox" id="fCap">'
                '시총 1조원 미만 숨기기</label>' if has_cap else "")
             + '<span class="count" id="cnt"></span>'
             '</div>',
             f'<div class="scroll"><table id="tAll"'
             f'{" class=clickable" if clickable else ""}><thead><tr>']
    parts.append(th("코드", "l pin") + th("종목명", "l") + th("업종", "l")
                 + th("부품군", "l") + th("시장", "l")
                 + (th("시가총액", "l", "종가 x 보통주 발행주식수") if has_cap else "")
                 + th("당월매출", "", "백만 NTD")
                 + th("MoM%", "", "당월 / 전월 - 1")
                 + th("YoY%", "", "당월 / 전년동월 - 1")
                 + th("YoY가속", "", "당월YoY - 전월YoY, 단위 %p")
                 + th("누계YoY%", "", "누계영업수익(당월) / 누계영업수익(전년 동월) - 1"))
    if p_ym:
        parts.append(th(p_lab, "", f"{p_ym} 공시 진행 중 · 발표분만 표시")
                     + th(f"{p_lab} 발표일", "",
                          "이 (종목, 월)이 DB에 처음 들어온 날. "
                          "추적 시작 이전 적재분은 —"))
    parts.append("</tr></thead><tbody>")
    for r in rows:
        mk = "TWSE" if r["market"] == "TWSE" else "TPEx"
        if r["is_ky"]:
            mk += " KY"
        s = f'{r["code"]} {name_plain(r)}'.lower()
        g = r["bom_group"] or ""
        pv = pend["vals"].get(r["code"]) if p_ym else None
        pdate = pend["dates"].get(r["code"]) if p_ym else None
        filed = pv is not None
        parts.append(
            f'<tr data-code="{esc(r["code"])}" data-s="{esc(s)}" '
            f'data-ind="{esc(r["industry_kr"])}" '
            f'data-mkt="{esc(mk)}" data-bom="{esc(g)}" data-rev="{r["rev_now"]}" '
            f'data-cap="{(r["cap"] or [0, 0])[1]:.4f}" '
            f'data-filed="{1 if filed else 0}">'
            f'<td class="l pin code" data-v="{esc(r["code"])}">{esc(r["code"])}</td>'
            f'<td class="l" data-v="{esc(r["name_kr"])}">{name_cell(r, with_biz=True)}</td>'
            # 업종 한자는 툴팁으로 남긴다: MOPS 등 대만 사이트에서 半導體業으로
            # 검색해야 할 때가 있어, 종목명 한자와 같은 이유로 정식 예외다.
            f'<td class="l ind" data-v="{esc(r["industry_kr"])}" '
            f'title="{esc(r["industry"])}">{esc(r["industry_kr"])}</td>'
            f'<td class="l bom" data-v="{esc(g)}">{esc(g)}</td>'
            f'<td class="l ind" data-v="{esc(mk)}">{esc(mk)}</td>'
            + (f'<td class="l cap" data-v="{(r["cap"] or [0])[0]:.4f}">'
               f'{fmt_cap(r["cap"])}</td>' if has_cap else "")
            + f'<td class="rev" data-v="{r["rev_now"]}">{mn(r["rev_now"])}</td>'
            f'<td class="{cls(r["mom"])}" data-v="{sort_key(r["mom"])}">{pc(r["mom"])}</td>'
            f'<td class="{cls(r["yoy"])}" data-v="{sort_key(r["yoy"])}">{pc(r["yoy"])}</td>'
            f'<td class="{cls(r["yoy_accel"])}" data-v="{sort_key(r["yoy_accel"])}">'
            f'{pc(r["yoy_accel"])}</td>'
            f'<td class="{cls(r["cum_yoy"])}" data-v="{sort_key(r["cum_yoy"])}">'
            f'{pc(r["cum_yoy"])}</td>')
        if p_ym:
            if filed:
                day = pdate[5:] if pdate else "&mdash;"
            else:
                day = ""
            parts.append(
                f'<td class="{"rev" if filed else "unfiled"}" '
                f'data-v="{pv if filed else ""}">'
                f'{mn(pv) if filed else "미발표"}</td>'
                f'<td class="ind" data-v="{esc(pdate or "")}">{day}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    if p_ym:
        dl, rolled = pend["deadline"], pend["rolled"]
        parts.append(
            f'<div class="note fn">· <b>{esc(p_lab)}</b> 컬럼은 아직 공시가 진행 중인 '
            f'{esc(p_ym)}분입니다. 본문의 다른 모든 수치는 완결월 {esc(ref_ym)} 기준이며 '
            '이 두 컬럼만 예외입니다'
            f'<br>· 발표일은 해당 (종목, 월)이 이 DB에 <b>처음 들어온 날</b>입니다. '
            '추적 기능 이전에 적재된 과거분은 <b>—</b>로 표시됩니다 &mdash; '
            '자료 생성일은 MOPS가 페이지를 만든 날이라 회사별 발표일로 쓸 수 없어 '
            '소급하지 않았습니다'
            f'<br>· 마감 <b>{esc(dl)}</b> (익월 10일'
            + ('이 주말이라 순연' if rolled else '')
            + '). <b>대만 공휴일 순연은 미반영</b>입니다'
            '</div>')
    return "\n".join(parts)


def render_movers(rows, ref_ym, sec=3):
    floor = groups.MIN_REV_FOR_MOVERS_K
    n = groups.MOVERS_N
    pool = [r for r in rows if r["rev_now"] is not None and r["rev_now"] >= floor]

    def panel(title, tag, key, reverse):
        got = [r for r in pool if r[key] is not None]
        got.sort(key=lambda r: r[key], reverse=reverse)
        sel = got[:n]
        out = [f'<div class="mv"><h3>{esc(title)}<span class="tag">{esc(tag)}</span></h3>',
               '<table><thead><tr>',
               '<th class="l">코드</th><th class="l">종목명</th>',
               '<th>당월매출</th><th>', esc("MoM%" if key == "mom" else "YoY%"), '</th>',
               '</tr></thead><tbody>']
        for r in sel:
            out.append(
                f'<tr><td class="l code">{esc(r["code"])}</td>'
                # non-BoM movers get their industry, not "(설명 미등록)"
                f'<td class="l">{name_cell(r)}'
                f'<div class="mvbiz">'
                f'{esc(r["biz"] if r["biz_registered"] else r["industry_kr"])}</div></td>'
                f'<td class="rev">{mn(r["rev_now"])}</td>'
                f'<td class="{cls(r[key])}">{pc(r[key])}</td></tr>')
        out.append("</tbody></table></div>")
        return "".join(out)

    excluded = len(rows) - len(pool)
    lo = min((r["rev_now"] for r in pool), default=0)
    return "\n".join([
        f'<h2><span class="n">{sec}</span>Top movers &mdash; {esc(ref_ym)}</h2>',
        f'<div class="note">당월매출 <b>{floor / 1000:,.0f}백만 NTD</b> 미만 '
        f'<b>{excluded}종목</b>은 제외했습니다. 분모가 작으면 %가 요동쳐 순위가 '
        f'무의미해집니다. 대상 <b>{len(pool)}종목</b>, 최소 매출 '
        f'<b>{mn(lo)}백만 NTD</b>. '
        f'&mdash; 기준선은 groups.py의 <code>MIN_REV_FOR_MOVERS_K</code>에서 조정합니다.</div>',
        '<div class="movers">',
        panel("YoY 상위", f"상위 {groups.MOVERS_N}", "yoy", True),
        panel("YoY 하위", f"하위 {groups.MOVERS_N}", "yoy", False),
        panel("MoM 상위", f"상위 {groups.MOVERS_N}", "mom", True),
        panel("MoM 하위", f"하위 {groups.MOVERS_N}", "mom", False),
        '</div>',
    ])


# --------------------------------------------------------------------------- #
# section 4: hand-rolled SVG mini charts
# --------------------------------------------------------------------------- #
# One <svg> per stock, hand-emitted. A charting library would ship its own
# runtime plus a JSON copy of every series; 37 <rect>s and one <polyline> per
# stock costs about a kilobyte.
CW, CH = 540, 300              # viewBox
PL, PR = 12, 528               # plot left / right
PT, PB = 62, 246               # plot top / bottom (above PT sits the label row)
BAR_FILL = ACCENT2             # #5B9BD5 bars    = monthly revenue
LINE_COL = ACCENT3             # #ED7D31 line    = YoY%
MOM_COL = ACCENT               # #FFCB05 line    = MoM%
MOM_CLIP = 60.0                # MoM 스케일은 ±60%에서 클리핑
MOM_SPAN = 0.40                # ±MOM_CLIP 이 플롯 높이의 이 비율만큼 차지

# 월별 숫자 라벨을 어디서 만들 것인가. --label-mode 로 바뀐다.
#   "svg" -- 차트마다 미리 그려 넣고 CSS로 숨긴다 (단순, 용량 큼)
#   "js"  -- 좌표계만 싣고 토글할 때 JS가 그린다 (모달용 SERIES 재사용, 용량 0)
LABEL_MODE = "js"


def months_axis(ref_ym: str, n: int) -> list[str]:
    return [shift_ym(ref_ym, -(n - 1 - i)) for i in range(n)]


def chart_svg(rec: dict, axis: list[str]) -> str:
    """Bar (revenue, 백만 NTD) + line (YoY%) over `axis`, drawn by hand.

    Each bar carries an SVG <title>, which browsers surface as a native tooltip
    on hover -- no JavaScript and no per-bar event listeners, which matters at
    100+ charts.
    """
    revs = [rec["rev"].get(ym) for ym in axis]
    yoys = [yoy_at(rec, ym) for ym in axis]
    moms = [pct(rec["rev"].get(ym), rec["rev"].get(shift_ym(ym, -1))) for ym in axis]

    vals = [v for v in revs if v is not None]
    rev_max = max(vals) if vals else 0
    n = len(axis)
    slot = (PR - PL) / n
    barw = max(3.0, slot * 0.74)

    def bar_y(v):
        if not rev_max:
            return PB
        return PB - (v / rev_max) * (PB - PT)

    # YoY gets its own scale, always including 0 so the sign is readable
    yv = [v for v in yoys if v is not None]
    lo = min(min(yv), 0.0) if yv else 0.0
    hi = max(max(yv), 0.0) if yv else 0.0
    if hi - lo < 1e-9:
        lo, hi = -1.0, 1.0
    pad = (hi - lo) * 0.10
    lo, hi = lo - pad, hi + pad

    def line_y(v):
        return PB - (v - lo) / (hi - lo) * (PB - PT)

    out = [f'<svg viewBox="0 0 {CW} {CH}" class="ch" role="img" '
           f'preserveAspectRatio="xMidYMid meet">']

    # plot frame + YoY zero line
    out.append(f'<rect x="{PL}" y="{PT}" width="{PR - PL}" height="{PB - PT}" '
               f'fill="none" stroke="#243039" stroke-width="1"/>')
    if lo < 0 < hi:
        zy = line_y(0.0)
        out.append(f'<line x1="{PL}" y1="{zy:.1f}" x2="{PR}" y2="{zy:.1f}" '
                   f'stroke="#3d4d5a" stroke-width="1" stroke-dasharray="4 4"/>')

    # bars, each with a hover tooltip
    for i, v in enumerate(revs):
        if v is None or v <= 0:
            continue
        x = PL + i * slot + (slot - barw) / 2
        y = bar_y(v)
        yo = yoys[i]
        tip = f"{axis[i]}  {mn(v)} 백만 NTD"
        if yo is not None:
            tip += f"  ·  YoY {yo:+,.1f}%"
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{barw:.1f}" '
                   f'height="{max(0.6, PB - y):.1f}" fill="{BAR_FILL}">'
                   f'<title>{esc(tip)}</title></rect>')

    # YoY line, broken wherever a month has no comparable prior year
    seg, segs = [], []
    for i, v in enumerate(yoys):
        if v is None:
            if len(seg) > 1:
                segs.append(seg)
            seg = []
            continue
        seg.append(f"{PL + i * slot + slot / 2:.1f},{line_y(v):.1f}")
    if len(seg) > 1:
        segs.append(seg)
    for s in segs:
        out.append(f'<polyline points="{" ".join(s)}" fill="none" '
                   f'stroke="{LINE_COL}" stroke-width="2.4" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
    if segs:
        lx, ly = segs[-1][-1].split(",")
        out.append(f'<circle cx="{lx}" cy="{ly}" r="3.6" fill="{LINE_COL}"/>')

    # MoM line. It shares the YoY zero line so both read against one baseline,
    # but gets its own fixed ±MOM_CLIP scale: MoM swings ±50% every month while
    # YoY reaches +300%, and on one scale the MoM line would be a flat smear.
    # A month past the clip is drawn at the clip and marked with a dot, so a
    # single spike cannot flatten the rest of the series.
    zy = line_y(0.0)
    half = (PB - PT) * MOM_SPAN

    def mom_y(v):
        c = max(-MOM_CLIP, min(MOM_CLIP, v))
        return max(PT + 1, min(PB - 1, zy - (c / MOM_CLIP) * half))

    mseg, msegs, mdots = [], [], []
    for i, v in enumerate(moms):
        if v is None:
            if len(mseg) > 1:
                msegs.append(mseg)
            mseg = []
            continue
        x = PL + i * slot + slot / 2
        y = mom_y(v)
        mseg.append(f"{x:.1f},{y:.1f}")
        if abs(v) > MOM_CLIP:
            mdots.append((x, y, v))
    if len(mseg) > 1:
        msegs.append(mseg)
    for s in msegs:
        out.append(f'<polyline class="momline" points="{" ".join(s)}" fill="none" '
                   f'stroke="{MOM_COL}" stroke-width="1.4" opacity="0.75" '
                   f'stroke-linejoin="round"/>')
    for x, y, v in mdots:
        out.append(f'<circle class="momdot" cx="{x:.1f}" cy="{y:.1f}" r="2.6" '
                   f'fill="{MOM_COL}" opacity="0.9">'
                   f'<title>MoM {v:+,.1f}% (±{MOM_CLIP:.0f}% 초과, 선은 클리핑)</title>'
                   f'</circle>')

    # label row: max revenue on the left, this month's MoM / YoY on the right
    mom, yoy = rec.get("mom"), rec.get("yoy")
    out.append(f'<text x="{PL}" y="30" class="cl cmax">{mn(rev_max)}</text>')
    out.append(f'<text x="{PL}" y="50" class="cs">최대 · 백만 NTD</text>')
    # values wear their line's colour so the header maps onto the chart at a
    # glance; the sign is still readable from the +/- prefix
    out.append(f'<text x="{PR}" y="30" class="cl" text-anchor="end">'
               f'<tspan class="cmom">{pc(mom) or "-"}</tspan>'
               f'<tspan class="csep"> / </tspan>'
               f'<tspan class="cyoy">{pc(yoy) or "-"}</tspan></text>')
    out.append(f'<text x="{PR}" y="50" class="cs" text-anchor="end">'
               f'MoM% / YoY%</text>')

    # sparse month ticks
    step = 6
    for i in range(0, n, step):
        ym = axis[i]
        out.append(f'<text x="{PL + i * slot + slot / 2:.1f}" y="{PB + 24}" '
                   f'class="ct" text-anchor="middle">{ym[2:4]}/{ym[5:7]}</text>')
    out.append(f'<text x="{PR}" y="{PB + 24}" class="ct" text-anchor="end">'
               f'{axis[-1][2:4]}/{axis[-1][5:7]}</text>')
    # --- 월별 숫자 라벨 -----------------------------------------------------
    # viewBox는 540 고정이고 1열 전체폭에서 약 3배로 확대되므로, CSS 11px로
    # 보이려면 user unit 기준 3.7이어야 한다. 3열일 때는 읽을 수 없는 크기지만
    # 라벨은 전체폭 모드에서만 켜지므로 상관없다.
    if LABEL_MODE == "svg":
        out.append(label_group("ly", yoys, line_y, slot, LINE_COL, dir_=-1))
        out.append(label_group("lm", moms, mom_y, slot, MOM_COL,
                               cls_extra=" momlbl", dir_=1))
    else:
        # 방식 B: 좌표계만 넘기고 라벨은 클릭 시 JS가 그린다. SERIES 데이터가
        # 이미 모달용으로 실려 있어 추가 용량이 사실상 없다.
        out.append("")

    out.append("</svg>")
    svg = "".join(out)
    if LABEL_MODE == "js":
        # JS가 파이썬과 똑같은 축을 재현하도록 스케일 파라미터를 실어 보낸다.
        # 축 계산을 JS에 복제하면 선과 라벨이 어긋나기 시작한다.
        sc = f"{PL:.1f},{slot:.4f},{rev_max},{lo:.4f},{hi:.4f},{zy:.2f},{half:.2f}"
        svg = svg.replace('<svg viewBox', f'<svg data-sc="{sc}" viewBox', 1)
    return svg


def label_group(cls: str, vals, y_of, slot: float, color: str,
                cls_extra: str = "", dir_: int = -1) -> str:
    """모든 달의 라벨을 찍는다. 홀수달은 LBL_ZIG 만큼 어긋나게 두 줄로 나눈다.

    JS 경로(drawLabels)와 배치 규칙이 같아야 --label-mode 비교가 의미를 갖는다.
    """
    placed: list[str] = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        txt = f"{v:+.0f}"
        w = len(txt) * LBL_FS * 0.62 + 3
        cx = PL + i * slot + slot / 2
        y = y_of(v) + (-2 if dir_ < 0 else LBL_H)
        if i % 2 == 1:
            y += dir_ * LBL_ZIG
        y = max(56.0, min(264.0, y))
        placed.append(
            f'<rect class="lbg" x="{cx - w / 2:.1f}" y="{y - LBL_H + 1.1:.1f}" '
            f'width="{w:.1f}" height="{LBL_H:.1f}"/>'
            f'<text class="lbt" x="{cx:.1f}" y="{y:.1f}">{txt}</text>')
    return f'<g class="lbl{cls_extra}">{"".join(placed)}</g>'


def render_charts(rows, ref_ym: str, n_months: int, stats: dict | None = None, sec=6):
    """Section 4: every bom_groups stock, one mini chart each, grouped by BoM layer."""
    by_code = {r["code"]: r for r in rows}
    axis = months_axis(ref_ym, n_months)
    rendered, missing = [], []

    if stats is not None:
        fb = blank = 0
        for c in bom_groups.all_codes():
            rec = by_code.get(c)
            if rec is None:
                continue
            for ym in axis:
                if rec["rev"].get(shift_ym(ym, -12)) is not None:
                    continue
                if rec["rev_ly"].get(ym) is not None:
                    fb += 1
                elif rec["rev"].get(ym) is not None:
                    blank += 1
        stats["chart_yoy_fallback"] = fb
        stats["chart_yoy_blank"] = blank

    body = []
    for gname, codes in bom_groups.GROUPS.items():
        present = [c for c in codes if c in by_code]
        missing += [c for c in codes if c not in by_code]
        if not present:
            continue
        rendered += present
        # scale goes in the label itself: a group totalling 2,500 has one stock
        # swinging its aggregate around, and the reader should see that up front
        gsum = sum(by_code[c]["rev_now"] or 0 for c in present)
        body.append(f'<h3 class="ghd">{esc(gname)}'
                    f'<span class="gn">({len(present)}종목 · 합계 {mn(gsum)} '
                    f'백만 NTD)</span></h3>')
        body.append('<div class="grid3">')
        for c in present:
            rec = by_code[c]
            body.append(
                f'<figure class="cbox" data-code="{esc(c)}" '
                f'title="클릭하면 월별 상세가 열립니다">'
                f'<figcaption class="chd">'
                f'<span class="code">{esc(c)}</span> {name_cell(rec)}'
                f'<div class="cind">{esc(rec["industry_kr"])}'
                + (f'<span class="ccap">{fmt_cap(rec["cap"])}</span>'
                   if rec.get("cap") else "")
                + '</div>'
                f'</figcaption>'
                f'{chart_svg(rec, axis)}'
                f'<div class="cbiz">{esc(rec["biz"])}</div>'
                f'</figure>')
        body.append('</div>')

    if stats is not None:
        stats["chart_points"] = len(rendered) * len(axis)
        stats["chart_stocks"] = len(rendered)
        stats["chart_missing"] = missing

    # The legend and the grouping rationale used to sit in two note boxes above
    # the grid. They pushed the first chart below the fold, so they live in the
    # ⓘ tooltip now -- still there to show a colleague, out of the way otherwise.
    tip = ('<b>부품군 편성 기준</b><br>'
           'AI 서버 한 대에 들어가는 부품 기준으로 묶었습니다. '
           '대만 공식 업종분류(반도체업·전자부품업 등)로는 '
           '파워(2308 델타)와 레일(2059 킹슬라이드)이 같은 전자부품업으로 묶여 '
           '공급망 흐름이 안 보이기 때문입니다. 편성은 <code>bom_groups.py</code>에서 '
           '관리하고, 소제목의 합계는 이중계상 제거 없는 단순합입니다.'
           '<br><br><b>차트 읽는 법</b><br>'
           f'<span style="color:{BAR_FILL}">■</span> 막대 = 월매출(백만 NTD), '
           '마우스를 올리면 해당 월 값<br>'
           f'<span style="color:{LINE_COL}">━</span> 굵은 선 = YoY%<br>'
           f'<span style="color:{MOM_COL}">━</span> 얇은 선 = MoM% '
           f'(±{MOM_CLIP:.0f}% 클리핑, 초과한 달은 점으로 표시)<br>'
           '막대·YoY·MoM은 각각 독립 스케일이고, 점선이 공통 0% 기준선입니다.'
           '<br><br><b>클릭</b> 하면 전체 이력 월별 상세표가 열립니다 '
           '(← → 로 같은 부품군 내 이동, ESC로 닫기).')
    if missing:
        tip += (f'<br><br>부품군에 등록됐으나 {esc(ref_ym)} 데이터가 없는 코드: '
                f'<b>{esc(", ".join(missing))}</b>')

    head = [f'<h2><span class="n">{sec}</span>AI서버 부품군별 종목 차트 '
            f'<span class="meta">{len(bom_groups.GROUPS)}개 부품군 · {len(rendered)}종목 · '
            f'{n_months}개월</span>'
            f'<span class="info" tabindex="0">&#9432;<span class="tip">{tip}</span></span>'
            f'</h2>',
            '<div class="thinbar">'
            '<label class="chk momtog"><input type="checkbox" id="momToggle" checked>'
            'MoM 선 표시</label>'
            '<label class="chk momtog"><input type="checkbox" id="numToggle">'
            '숫자 표시</label>'
            '</div>']
    return "\n".join(head + body)


# --------------------------------------------------------------------------- #
# section 4: group YoY heatmap
# --------------------------------------------------------------------------- #
HEAT_CLIP = 40.0            # 색 농도가 포화하는 지점. 숫자는 자르지 않는다.
HEAT_BASE = (20, 27, 34)
# 문서 전체와 같은 규칙: 플러스 = 주황(ACCENT3), 마이너스 = 파랑(ACCENT2).
# 히트맵만 초록/빨강을 쓰면 같은 페이지에서 +70이 위에선 초록, 아래 표에선
# 주황으로 나와 반드시 오독된다.
HEAT_POS = (237, 125, 49)   # #ED7D31
HEAT_NEG = (91, 155, 213)   # #5B9BD5
HEAT_CLIP_MOM = 25.0        # MoM 탭. YoY의 ±40을 그대로 쓰면 전부 회색이 된다.
HEAT_CLIP_QOQ = 30.0        # 3개월 합 QoQ. MoM보다 진폭이 크고 YoY보다 작다.
HEAT_ZERO_EPS = 0.5         # 정수 표기라 이 아래는 '0'으로 보고 회색
SMALL_GROUP_K = 3_000_000   # 합계 3,000 백만 NTD 미만이면 노이즈 경고


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def heat_style(v, clip: float = HEAT_CLIP) -> tuple[str, str, str]:
    """(background, colour, extra class) for a change value.

    Density saturates at ±clip but the printed number stays true, so a cell at
    +316 looks the same as one at +40. Over-clip cells therefore get white bold
    text -- the signal for 'colour is maxed out, the real value is larger'.

    The clip differs per tab: MoM swings in a far narrower band than YoY, so
    reusing ±40 there would render the whole grid a flat grey.
    """
    if v is None:
        return "#141b22", "#4a5661", ""
    if abs(v) < HEAT_ZERO_EPS:
        return "#151c23", "#93a4b1", ""
    c = max(-clip, min(clip, v))
    t = abs(c) / clip
    rgb = _lerp(HEAT_BASE, HEAT_POS if c > 0 else HEAT_NEG, t)
    over = abs(v) > clip
    fg = "#ffffff" if (over or t > 0.42) else "#b9c7d2"
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})", fg, "sat" if over else ""


def _window_sum(rec: dict, ym: str, window: int):
    """ym에서 끝나는 window개월 합. 한 달이라도 비면 None."""
    if window == 1:
        return rec["rev"].get(ym)
    vals = [rec["rev"].get(shift_ym(ym, -i)) for i in range(window)]
    return None if any(v is None for v in vals) else sum(vals)


def group_change(by_code: dict, members: list[str], ym: str, lag: int,
                 window: int = 1) -> dict:
    """like-for-like change for a set of codes at one month.

    (window, lag) = (1, 12) YoY / (1, 1) MoM / (3, 3) 3개월합 QoQ.
    QoQ는 변곡 점검의 qoq_3m과 같은 공식이다: 해당 월 포함 직전 3개월 합을
    그 이전 3개월 합과 비교한다.

    비교 구간 양쪽에 자료가 다 있는 종목만 합산한다(like-for-like). 그러지
    않으면 중간에 상장한 종목(7769는 2024-07 시작이고 소속 부품군의 약 31%)
    때문에 12개월 내내 합계가 뛰어, 실제 성장이 없는데도 칸에 불이 들어온다.

    lfl_sum_* 가 퍼센트를 만들고, total_sum은 전 종목 단순합이라 '부품군 매출'
    라벨에 쓰는 값이다. 둘은 다르므로 이름을 갈라 놓았다.
    """
    prev = shift_ym(ym, -lag)
    lfl_now = lfl_prev = 0
    total_sum = 0
    n_lfl = 0
    for c in members:
        rec = by_code.get(c)
        if rec is None:
            continue
        a = _window_sum(rec, ym, window)
        b = _window_sum(rec, prev, window)
        cur = rec["rev"].get(ym)
        if cur is not None:
            total_sum += cur
        if a is not None and b is not None:
            lfl_now += a
            lfl_prev += b
            n_lfl += 1
    return {"chg": pct(lfl_now, lfl_prev) if n_lfl else None,
            "lfl_sum": lfl_now, "lfl_sum_prev": lfl_prev,
            "total_sum": total_sum,
            "n_lfl": n_lfl, "n_pool": len(members)}


def heat_table(by_code: dict, axis: list[str], ref_ym: str,
               lag: int, clip: float, window: int = 1) -> str:
    """One heatmap grid. (window, lag): (1,12) YoY / (1,1) MoM / (3,3) QoQ.

    All tabs share rows, row order, row labels and columns so that switching
    between them changes only the colour field -- that is the whole point of
    putting them in the same place.
    """
    if window > 1:
        cur_lbl, base_lbl = f"최근{window}개월", f"직전{window}개월"
    else:
        cur_lbl, base_lbl = "당월", ("전년" if lag == 12 else "전월")

    def cell(d):
        v = d["chg"]
        bg, fg, sat = heat_style(v, clip)
        if v is None:
            txt = ""
        elif abs(v) < HEAT_ZERO_EPS:
            txt = "0"                      # '+0' / '-0' 을 피한다
        else:
            txt = f"{v:+.0f}"
        klass = "hc" + (" part" if d["n_lfl"] < d["n_pool"] else "") \
                     + (" " + sat if sat else "")
        tip = (f"부품군 {d['n_pool']}종목 중 {d['n_lfl']}종목 · "
               f"{cur_lbl} {mn(d['lfl_sum'])} → {base_lbl} {mn(d['lfl_sum_prev'])}"
               if d["n_lfl"] else "합산 가능한 종목 없음")
        if sat:
            tip += f"  (색 포화: 실제 {v:+,.1f}%)"
        return (f'<td class="{klass}" style="background:{bg};color:{fg}" '
                f'title="{esc(tip)}">{txt}</td>')

    def row(label, sub, members, extra=""):
        cells = [cell(group_change(by_code, members, ym, lag, window))
                 for ym in axis]
        return (f'<tr class="{extra}"><th class="hl">{esc(label)}'
                f'<span class="hsub">{esc(sub)}</span></th>{"".join(cells)}</tr>')

    out = ['<div class="hscroll"><table class="hm"><thead><tr>',
           '<th class="hl">부품군</th>']
    for i, ym in enumerate(axis):
        last = i == len(axis) - 1
        out.append(f'<th class="hmth{" refm" if last else ""}">'
                   f'{ym[2:4]}/{ym[5:7]}'
                   + ('<span class="rtag">기준월</span>' if last else "")
                   + '</th>')
    out.append("</tr></thead><tbody>")

    # benchmark first: without it there is no way to tell whether a group is
    # simply riding the cycle or actually outrunning it
    bench = bom_groups.benchmark_members()
    b_now = group_change(by_code, bench, ref_ym, lag, window)
    out.append(row(f"부품군 {len(bom_groups.all_codes())} 합계",
                   f"모자 제외 {len(bench)} · {mn(b_now['total_sum'])}",
                   bench, extra="bench"))

    for stage, gnames in bom_groups.STAGES.items():
        out.append(f'<tr class="stg"><td colspan="{len(axis) + 1}">'
                   f'{esc(stage)}</td></tr>')
        for g in gnames:
            if g not in bom_groups.GROUPS:
                continue
            members = bom_groups.heatmap_members(g)
            now = group_change(by_code, members, ref_ym, lag, window)
            small = now["total_sum"] < SMALL_GROUP_K
            sub = f"{len(bom_groups.GROUPS[g])}종목 · {mn(now['total_sum'])}"
            out.append(row(g, sub, members, extra="tiny" if small else ""))
    out.append("</tbody></table></div>")
    return "".join(out)


def render_heatmap(rows, ref_ym: str, n_months: int, sec=2):
    by_code = {r["code"]: r for r in rows}
    axis = months_axis(ref_ym, n_months)

    common = ('· 부품군 합계는 모자 이중계상 제거 기준입니다'
              '<br>· 일부만 합산된 셀은 하단에 작은 점(·)이 붙습니다'
              f'<br>· 합계 {SMALL_GROUP_K // 1000:,} 백만 미만 부품군'
              '(행 라벨이 어두운 행)은 한두 종목이 전체를 좌우합니다')

    fn_yoy = (
        '<div class="note fn hmfn" data-pane="yoy">'
        + common
        + '<br>· YoY는 당월·전년동월 양쪽에 자료가 있는 종목만 '
        '합산(like-for-like)합니다. 상장·편입 시점 차이로 인한 왜곡을 제거한 값입니다'
        f'<br>· 색 농도는 ±{HEAT_CLIP:.0f}%에서 포화합니다. '
        f'<b class="satlegend">흰 굵은 글씨</b> = ±{HEAT_CLIP:.0f}% 초과 &mdash; '
        '색은 최대치지만 실제값은 더 큽니다. 숫자는 클리핑하지 않은 실제값입니다'
        '</div>')

    fn_mom = (
        '<div class="note fn hmfn" data-pane="mom" hidden>'
        + common
        + '<br>· MoM은 당월·전월 양쪽에 자료가 있는 종목만 '
        '합산(like-for-like)합니다'
        f'<br>· 색 농도는 ±{HEAT_CLIP_MOM:.0f}%에서 포화합니다(YoY 탭은 '
        f'±{HEAT_CLIP:.0f}%). MoM은 변동폭이 훨씬 좁아 같은 기준을 쓰면 '
        '전체가 회색이 됩니다. <b class="satlegend">흰 굵은 글씨</b> = '
        f'±{HEAT_CLIP_MOM:.0f}% 초과'
        '<br>· <b>MoM은 계절성 보정이 없습니다.</b> 대만은 춘절이 낀 달의 매출이 '
        '구조적으로 급감하고 그 다음 달이 급반등하므로, 매년 1~2월 전후로 '
        '파랑→주황 패턴이 반복됩니다. 춘절 날짜는 해마다 달라 특정 열로 '
        '고정되지 않습니다. <b>계절 패턴을 깨는 달이 실제 신호입니다.</b>'
        '</div>')

    fn_qoq = (
        '<div class="note fn hmfn" data-pane="qoq" hidden>'
        + common
        + '<br>· 3개월 합계 롤링 QoQ입니다. 해당 월 포함 직전 3개월 합을 그 이전 '
        '3개월 합과 비교하며, 6개월 전부 자료가 있는 종목만 합산합니다'
        f'<br>· 색 농도는 ±{HEAT_CLIP_QOQ:.0f}%에서 포화합니다'
        f'(YoY ±{HEAT_CLIP:.0f}%, MoM ±{HEAT_CLIP_MOM:.0f}%). '
        '<b class="satlegend">흰 굵은 글씨</b> = 초과'
        '<br>· <b>3개월 합계를 굴려서 비교하므로 MoM보다 계절성이 크게 줄지만 '
        '완전히 사라지지는 않습니다.</b> 춘절이 낀 1~3월이 분모가 되는 4~6월 열은 '
        '구조적으로 높게 나옵니다. <b>계절 패턴을 거스르는 칸이 실제 신호입니다.</b>'
        '</div>')

    return "\n".join([
        f'<h2><span class="n">{sec}</span>부품군 히트맵 '
        f'<span class="meta">{len(bom_groups.GROUPS)}개 부품군 · 최근 {n_months}개월 '
        f'({axis[0]} ~ {axis[-1]})</span>'
        '<span class="tabs">'
        '<button class="tab on" data-pane="yoy">YoY</button>'
        '<button class="tab" data-pane="mom">MoM</button>'
        '<button class="tab" data-pane="qoq">QoQ</button>'
        '</span></h2>',
        f'<div class="hmpane" data-pane="yoy">'
        f'{heat_table(by_code, axis, ref_ym, 12, HEAT_CLIP)}</div>',
        f'<div class="hmpane" data-pane="mom" hidden>'
        f'{heat_table(by_code, axis, ref_ym, 1, HEAT_CLIP_MOM)}</div>',
        f'<div class="hmpane" data-pane="qoq" hidden>'
        f'{heat_table(by_code, axis, ref_ym, 3, HEAT_CLIP_QOQ, window=3)}</div>',
        fn_yoy, fn_mom, fn_qoq,
    ])


# --------------------------------------------------------------------------- #
# section 5: inflection check
# --------------------------------------------------------------------------- #
def qoq_3m(rec: dict, ref_ym: str):
    """최근 3개월 합 ÷ 직전 3개월 합 - 1. 한 달이라도 비면 None.

    히트맵 QoQ 탭과 같은 공식이라 _window_sum을 함께 쓴다. 두 곳에 따로
    적으면 한쪽만 고쳐져 값이 갈린다.
    """
    return pct(_window_sum(rec, ref_ym, 3),
               _window_sum(rec, shift_ym(ref_ym, -3), 3))


def abs_trend(rec: dict, ref_ym: str) -> tuple[str, float | None]:
    """전체 이력 최대값 대비 현재 위치."""
    vals = [v for v in rec["rev"].values() if v is not None]
    cur = rec["rev"].get(ref_ym)
    if not vals or cur is None:
        return "", None
    mx = max(vals)
    if cur >= mx:
        return "사상최대", 0.0
    off = (cur / mx - 1) * 100 if mx else None
    return (f"고점대비 {off:,.0f}%" if off is not None else ""), off


def render_inflection(rows, ref_ym: str, sec=5):
    by_code = {r["code"]: r for r in rows}
    items = []
    for c in bom_groups.all_codes():
        rec = by_code.get(c)
        if rec is None or rec["rev_now"] is None:
            continue
        trend, off = abs_trend(rec, ref_ym)
        items.append({**rec, "qoq": qoq_3m(rec, ref_ym), "trend": trend,
                      "trend_off": off, "months": len(rec["rev"])})

    down = sorted([x for x in items if x["yoy"] is not None and x["yoy"] < 0],
                  key=lambda x: x["yoy"])
    slow = sorted([x for x in items if x["yoy"] is not None and x["yoy"] >= 0
                   and x["qoq"] is not None and x["qoq"] < 0],
                  key=lambda x: x["qoq"])
    grow = sorted([x for x in items if x["yoy"] is not None and x["yoy"] >= 0
                   and x["qoq"] is not None and x["qoq"] >= 0],
                  key=lambda x: -x["qoq"])[:5]

    q_from = f"{shift_ym(ref_ym, -2)}~{ref_ym}"
    q_to = f"{shift_ym(ref_ym, -5)}~{shift_ym(ref_ym, -3)}"
    short = [x for x in items if x["months"] < 36]

    parts = [f'<h2><span class="n">{sec}</span>변곡 점검 '
             f'<span class="meta">부품군 {len(items)}종목 · 3M합 QoQ = '
             f'{q_from} ÷ {q_to}</span></h2>']

    def block(icon, title, sel, note=""):
        out = [f'<h3 class="ihd">{icon} {esc(title)} '
               f'<span class="gn">{len(sel)}종목</span>'
               + (f'<span class="inote">{esc(note)}</span>' if note else "")
               + '</h3>']
        if not sel:
            out.append('<div class="none">해당 없음</div>')
            return "".join(out)
        out.append('<div class="scroll"><table class="itab"><thead><tr>')
        out.append('<th class="l pin">코드</th><th class="l">한글명</th>'
                   '<th class="l">그룹</th><th>최신월매출</th><th>YoY%</th>'
                   '<th>3M합 QoQ%</th><th class="l">절대추세</th>'
                   '<th>이력</th><th class="l">사업</th>')
        out.append("</tr></thead><tbody>")
        for x in sel:
            hist = f'{x["months"]}개월'
            hcls = "hshort" if x["months"] < 36 else "ind"
            out.append(
                f'<tr>'
                f'<td class="l pin code">{esc(x["code"])}</td>'
                f'<td class="l">{name_cell(x)}</td>'
                f'<td class="l bom">{esc(x["bom_group"] or "")}</td>'
                f'<td class="rev">{mn(x["rev_now"])}</td>'
                f'<td class="{cls(x["yoy"])}">{pc(x["yoy"])}</td>'
                f'<td class="{cls(x["qoq"])}">{pc(x["qoq"])}</td>'
                f'<td class="l {"peak" if x["trend"] == "사상최대" else "ind"}">'
                f'{esc(x["trend"])}</td>'
                f'<td class="{hcls}">{hist}</td>'
                f'<td class="l biz">{esc(x["biz"])}</td>'
                f'</tr>')
        out.append("</tbody></table></div>")
        return "".join(out)

    parts.append(block("🔴", "역성장", down, "YoY 마이너스 · YoY 낮은 순"))
    parts.append(block("🟡", "시퀀셜 둔화", slow,
                       "YoY는 플러스인데 3M합 QoQ 마이너스 · QoQ 낮은 순"))
    parts.append(block("🟢", "성장 지속", grow, "YoY·QoQ 모두 플러스 중 QoQ 상위 5"))

    parts.append(
        '<div class="note fn">'
        '· <b>3M합 QoQ는 계절성을 보정하지 않습니다.</b> 1~3월에는 춘절 연휴가 '
        '포함되므로 4~6월÷1~3월은 구조적으로 높게 나오고, 반대 분기는 낮게 나옵니다. '
        f'현재 기준월({esc(ref_ym)})에서는 <b>🟡 시퀀셜 둔화가 특히 강한 신호</b>입니다 '
        '&mdash; 계절적으로 유리한 국면인데도 3개월 합이 줄었다는 뜻입니다'
        '<br>· 절대추세는 전체 이력(최대 48개월) 최대값 대비 위치입니다. '
        + (f'이력이 36개월 미만인 <b>{len(short)}종목</b>'
           f'({", ".join(f"{x['code']} {x['name_kr']} {x['months']}개월" for x in short)})'
           '은 "사상최대"라도 비교 구간이 짧아 신호가 약합니다'
           if short else "이력이 짧은 종목은 없습니다")
        + '<br>· 세 분류는 상호배타적입니다. 🔴는 YoY 기준, 🟡·🟢는 YoY 플러스 안에서 '
        'QoQ 부호로 갈립니다'
        '</div>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# monthly-detail modal: data only, rendered on demand
# --------------------------------------------------------------------------- #
def modal_payload(rows, codes: list[str], lo_ym: str, ref_ym: str,
                  disc: dict | None = None, pend_ym: str = "",
                  deadline: str = "", pend_rev: dict | None = None,
                  pend_cum: dict | None = None) -> tuple[str, int]:
    """Series + identity for the click-through modal, as compact JSON.

    Pre-rendering 100+ full-history tables would add hundreds of KB of markup
    that almost never gets looked at. Instead the raw 千元 series ships once and
    the modal computes MoM / YoY / 누계YoY in the browser. 累計 cannot be derived
    from the monthly figures (a 정정공시 can move revenue between months without
    restating the cumulative), so it ships alongside.
    """
    n = 0
    end = pend_ym or ref_ym          # 모달은 진행 중인 달까지 전부 보여준다
    y, m = int(lo_ym[:4]), int(lo_ym[5:7])
    axis = []
    while f"{y:04d}-{m:02d}" <= end:
        axis.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    disc = disc or {}
    by_code = {r["code"]: r for r in rows}
    ser, cum, met = {}, {}, {}
    for c in codes:
        rec = by_code.get(c)
        if rec is None:
            continue
        ser[c] = [rec["rev"].get(ym) for ym in axis]
        cum[c] = [rec["cum"].get(ym) for ym in axis]
        if pend_ym:
            # rec stops at the reference month; splice the in-progress month on
            ser[c][-1] = (pend_rev or {}).get(c)
            cum[c][-1] = (pend_cum or {}).get(c)
        met[c] = {"k": rec["name_kr"], "e": rec.get("name_en") or "",
                  "z": rec.get("name") or "", "i": rec["industry_kr"],
                  "g": rec["bom_group"] or "",
                  "b": rec["biz"] if rec.get("biz_registered") else ""}
        n += 1
    j = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    payload = (f'const SERIES_START={j(lo_ym)};\n'
               f'const SERIES={j(ser)};\n'
               f'const SERIES_CUM={j(cum)};\n'
               f'const SMETA={j(met)};\n'
               f'const SDISC={j(disc)};\n'
               f'const PEND_YM={j(pend_ym or "")};\n'
               f'const PEND_DEADLINE={j(deadline or "")};\n')
    return payload, n


MODAL_HTML = """
<div id="mdBack" class="mdback" hidden>
  <div class="mdbox" role="dialog" aria-modal="true">
    <div class="mdhead">
      <div class="mdtitle">
        <span class="mdcode"></span>
        <span class="mdkr"></span>
        <span class="mden"></span>
        <span class="mdzh"></span>
      </div>
      <div class="mdtags"><span class="mdind"></span><span class="mdgrp"></span></div>
      <div class="mdbiz"></div>
    </div>
    <div class="mdbar">
      <button class="mdnav" id="mdPrev" title="이전 종목 (←)">&#9664;</button>
      <span class="mdpos"></span>
      <button class="mdnav" id="mdNext" title="다음 종목 (→)">&#9654;</button>
      <button class="mdcsv" id="mdCsv">CSV 복사</button>
      <span class="mdmsg"></span>
      <button class="mdx" id="mdClose" title="닫기 (ESC)">&#10005;</button>
    </div>
      <div class="mddisc"></div>
    <div class="scroll mdscroll"><table class="mdtab"><thead><tr>
      <th class="l">년월</th><th>매출</th><th>MoM%</th><th>YoY%</th><th>누계YoY%</th>
      <th class="l">발표일</th>
    </tr></thead><tbody></tbody></table></div>
  </div>
</div>
"""

MODAL_JS = """
var MD = { list: [], idx: 0, code: null };
function _pct(a,b){ if(a==null||b==null||b<=0) return null; return (a/b-1)*100; }
function _mn(v){ if(v==null) return ''; if(v===0) return '0';
  if(Math.abs(v)<500) return v>0?'<1':'>-1'; return Math.round(v/1000).toLocaleString(); }
function _pc(v){ if(v==null) return ''; if(Math.abs(v)<0.05) return '0.0';
  return (v>0?'+':'') + v.toFixed(1); }
function _cls(v){ if(v==null) return 'na'; if(Math.abs(v)<0.05) return 'flat';
  return v>0?'up':'dn'; }
function _ym(i){
  var y = parseInt(SERIES_START.slice(0,4),10), m = parseInt(SERIES_START.slice(5,7),10);
  var t = y*12 + (m-1) + i;
  return ('000'+Math.floor(t/12)).slice(-4) + '-' + ('0'+(t%12+1)).slice(-2);
}
function mdRows(code){
  var s = SERIES[code], c = SERIES_CUM[code], d = SDISC[code] || {}, out = [];
  for (var i = s.length-1; i >= 0; i--){
    var ym = _ym(i);
    out.push({ ym:ym, rev:s[i],
      mom:_pct(s[i], i>=1 ? s[i-1] : null),
      yoy:_pct(s[i], i>=12 ? s[i-12] : null),
      cum:_pct(c[i], i>=12 ? c[i-12] : null),
      disc: d[ym] || null, has: s[i] != null });
  }
  return out;
}
// header line: when did this stock last file, and has it filed for the
// in-progress month yet?
function mdDiscLine(code){
  var d = SDISC[code] || {}, s = SERIES[code];
  var pendIdx = s.length - 1;
  var filedPend = PEND_YM && s[pendIdx] != null;
  if (PEND_YM && !filedPend){
    return { txt: PEND_YM.slice(2).replace('-','/') + ' 미발표 · 마감 '
                  + PEND_DEADLINE.slice(5).replace('-','/'), cls:'pend' };
  }
  var keys = Object.keys(d).sort();
  if (!keys.length) return { txt:'발표일 이력 없음 (추적 시작 이전 적재분)', cls:'none' };
  var last = keys[keys.length-1];
  return { txt: '최근 발표 ' + d[last] + ' (' + last.slice(2).replace('-','/') + '분)',
           cls:'ok' };
}
function openModal(code, list){
  if (!SERIES[code]) return;
  MD.code = code;
  MD.list = (list && list.length) ? list.filter(function(x){return !!SERIES[x];}) : [code];
  MD.idx = Math.max(0, MD.list.indexOf(code));
  var m = SMETA[code], b = document.getElementById('mdBack');
  b.querySelector('.mdcode').textContent = code;
  b.querySelector('.mdkr').textContent = m.k;
  b.querySelector('.mden').textContent = m.e;
  b.querySelector('.mdzh').textContent = m.z;
  b.querySelector('.mdind').textContent = m.i;
  var g = b.querySelector('.mdgrp');
  g.textContent = m.g; g.style.display = m.g ? '' : 'none';
  var bz = b.querySelector('.mdbiz');
  bz.textContent = m.b; bz.style.display = m.b ? '' : 'none';
  b.querySelector('.mdpos').textContent = (MD.idx+1) + ' / ' + MD.list.length;
  b.querySelector('.mdmsg').textContent = '';
  var dl = mdDiscLine(code), dv = b.querySelector('.mddisc');
  dv.textContent = dl.txt; dv.className = 'mddisc ' + dl.cls;
  var tb = b.querySelector('.mdtab tbody'), html = '';
  mdRows(code).forEach(function(r){
    var disc = r.disc ? r.disc.slice(5) : (r.has ? '&mdash;' : '');
    html += '<tr' + (r.ym === PEND_YM ? ' class="pendrow"' : '') + '>'
          + '<td class="l">' + r.ym + '</td>'
          + '<td class="rev">' + (r.has ? _mn(r.rev) : '<span class="unfiled">미발표</span>') + '</td>'
          + '<td class="' + _cls(r.mom) + '">' + _pc(r.mom) + '</td>'
          + '<td class="' + _cls(r.yoy) + '">' + _pc(r.yoy) + '</td>'
          + '<td class="' + _cls(r.cum) + '">' + _pc(r.cum) + '</td>'
          + '<td class="l ind">' + disc + '</td></tr>';
  });
  tb.innerHTML = html;
  b.hidden = false;
  b.querySelector('.mdscroll').scrollTop = 0;
  document.body.style.overflow = 'hidden';
}
function closeModal(){
  document.getElementById('mdBack').hidden = true;
  document.body.style.overflow = '';
}
function mdStep(d){
  if (MD.list.length < 2) return;
  MD.idx = (MD.idx + d + MD.list.length) % MD.list.length;
  openModal(MD.list[MD.idx], MD.list);
}
function mdCsv(){
  var head = ['년월','매출(백만NTD)','MoM%','YoY%','누계YoY%','발표일'].join('\\t');
  var body = mdRows(MD.code).map(function(r){
    return [r.ym, r.rev==null?'':Math.round(r.rev/1000),
            r.mom==null?'':r.mom.toFixed(1),
            r.yoy==null?'':r.yoy.toFixed(1),
            r.cum==null?'':r.cum.toFixed(1),
            r.disc||''].join('\\t');
  }).join('\\n');
  var txt = MD.code + '\\t' + SMETA[MD.code].k + '\\n' + head + '\\n' + body;
  var msg = document.querySelector('#mdBack .mdmsg');
  function ok(){ msg.textContent = '복사됨 (' + (SERIES[MD.code].length) + '개월)'; }
  function fail(){ msg.textContent = '복사 실패 — 표를 직접 선택해 주세요'; }
  // file:// is not always a secure context, so keep the textarea fallback
  if (navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(txt).then(ok, function(){ legacy(); });
  } else { legacy(); }
  function legacy(){
    var ta = document.createElement('textarea');
    ta.value = txt; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy') ? ok() : fail(); } catch(e){ fail(); }
    document.body.removeChild(ta);
  }
}
function wireModal(){
  var b = document.getElementById('mdBack');
  if (!b) return;
  b.addEventListener('click', function(e){ if (e.target === b) closeModal(); });
  document.getElementById('mdClose').addEventListener('click', closeModal);
  document.getElementById('mdPrev').addEventListener('click', function(){ mdStep(-1); });
  document.getElementById('mdNext').addEventListener('click', function(){ mdStep(1); });
  document.getElementById('mdCsv').addEventListener('click', mdCsv);
  document.addEventListener('keydown', function(e){
    if (b.hidden) return;
    if (e.key === 'Escape') closeModal();
    else if (e.key === 'ArrowLeft') mdStep(-1);
    else if (e.key === 'ArrowRight') mdStep(1);
  });
  // charts: navigate within the stock's own BoM group
  document.querySelectorAll('.cbox[data-code]').forEach(function(fig){
    fig.addEventListener('click', function(){
      var grid = fig.closest('.grid3');
      var list = grid ? Array.prototype.map.call(grid.querySelectorAll('.cbox[data-code]'),
                        function(x){ return x.dataset.code; }) : null;
      openModal(fig.dataset.code, list);
    });
  });
  // table rows: navigate within whatever is currently visible, in sort order
  document.querySelectorAll('table.clickable tbody').forEach(function(tb){
    tb.addEventListener('click', function(e){
      var tr = e.target.closest('tr');
      if (!tr || !tr.dataset.code || !SERIES[tr.dataset.code]) return;
      var list = Array.prototype.filter.call(tb.rows, function(x){
        return !x.classList.contains('hide') && SERIES[x.dataset.code];
      }).map(function(x){ return x.dataset.code; });
      openModal(tr.dataset.code, list);
    });
  });
}
function wireMomToggle(){
  var cb = document.getElementById('momToggle');
  if (!cb) return;
  function run(){ document.body.classList.toggle('hide-mom', !cb.checked); }
  cb.addEventListener('change', run); run();
}
// Month labels. Drawn on demand from SERIES (already shipped for the modal) plus
// the per-chart scale in data-sc, so the axes match the Python-drawn lines
// exactly instead of being re-derived and drifting.
var LBL_H = __LBL_H__, LBL_FS = __LBL_FS__, LBL_ZIG = __LBL_ZIG__;
var NS = 'http://www.w3.org/2000/svg';
function lblPct(a, b){ if(a==null||b==null||b<=0) return null; return (a/b-1)*100; }
function drawLabels(svg, nMonths){
  if (svg.dataset.lbl === '1') return;          // already built
  var sc = (svg.dataset.sc||'').split(',').map(Number);
  var code = svg.closest('.cbox') && svg.closest('.cbox').dataset.code;
  if (!code || !SERIES[code] || sc.length < 7) return;
  var PL=sc[0], slot=sc[1], loY=sc[3], hiY=sc[4], zy=sc[5], half=sc[6];
  var s = SERIES[code];
  // the chart window is the last nMonths ending at the reference month, and
  // the reference month is the last complete one -- one before the pending
  var end = PEND_YM ? s.length - 2 : s.length - 1;
  var start = end - nMonths + 1;
  var yoy = [], mom = [];
  for (var i = start; i <= end; i++){
    yoy.push(lblPct(s[i], i>=12 ? s[i-12] : null));
    mom.push(lblPct(s[i], i>=1  ? s[i-1]  : null));
  }
  function yLine(v){ return 246 - (v-loY)/(hiY-loY)*(246-62); }
  function yMom(v){
    var c = Math.max(-60, Math.min(60, v));
    return Math.max(63, Math.min(245, zy - (c/60)*half));
  }
  // dir -1: 선 위쪽, +1: 선 아래쪽.
  // 모든 달을 다 찍는다. 라벨 폭(약 21)이 한 달 폭(14.3)보다 넓어 겹치므로
  // 홀수달을 LBL_ZIG 만큼 어긋나게 놓아 두 줄로 나눈다.
  function group(vals, yOf, extra, dir){
    var g = document.createElementNS(NS,'g');
    g.setAttribute('class','lbl'+(extra||''));
    var out = [];
    for (var i = 0; i < vals.length; i++){
      var v = vals[i]; if (v == null) continue;
      var txt = (v>0?'+':'') + Math.round(v);
      var w = txt.length*LBL_FS*0.62 + 3;
      var cx = PL + i*slot + slot/2;
      var y = yOf(v) + (dir < 0 ? -2 : LBL_H);
      if (i % 2 === 1) y += dir * LBL_ZIG;
      y = Math.max(56, Math.min(264, y));
      out.push([cx - w/2, y, w, cx, txt]);
    }
    out.forEach(function(o){
      var r = document.createElementNS(NS,'rect');
      r.setAttribute('class','lbg'); r.setAttribute('x',o[0].toFixed(1));
      r.setAttribute('y',(o[1]-LBL_H+1.1).toFixed(1));
      r.setAttribute('width',o[2].toFixed(1)); r.setAttribute('height',LBL_H);
      var t = document.createElementNS(NS,'text');
      t.setAttribute('class','lbt'); t.setAttribute('x',o[3].toFixed(1));
      t.setAttribute('y',o[1].toFixed(1));   // 색은 CSS(.lbt / .momlbl .lbt)
      t.textContent = o[4];
      g.appendChild(r); g.appendChild(t);
    });
    return g;
  }
  svg.appendChild(group(yoy, yLine, '', -1));
  svg.appendChild(group(mom, yMom, ' momlbl', 1));
  svg.dataset.lbl = '1';
}
function wireNumToggle(nMonths){
  var cb = document.getElementById('numToggle');
  if (!cb) return;
  function run(){
    var on = cb.checked;
    document.body.classList.toggle('show-nums', on);   // 그리드는 3열 그대로
    if (on) document.querySelectorAll('svg.ch').forEach(function(s){
      drawLabels(s, nMonths);
    });
  }
  cb.addEventListener('change', run); run();
}
// 타임라인 필터. 발표 목록과 미발표 접기 목록에 같이 걸고, 위의 큰 숫자도 갱신한다.
function wireTimeline(capFloorJo){
  var bom = document.getElementById('tlBom');
  var cap = document.getElementById('tlCap');
  var t = document.getElementById('tTL'), u = document.getElementById('tTLU');
  if (!bom || !t) return;
  var rowsF = Array.prototype.slice.call(t.tBodies[0].rows);
  var rowsU = u ? Array.prototype.slice.call(u.tBodies[0].rows) : [];
  function run(){
    var onlyBom = bom.checked;
    // 부품군 113종목은 직접 고른 것이라 시총으로 거르지 않는다 -- 종목 표와 같은 규칙
    var capMin = (cap && cap.checked && !onlyBom) ? capFloorJo : 0;
    if (cap){
      cap.disabled = onlyBom;
      var lab = cap.closest('.chk');
      if (lab) lab.classList.toggle('off', onlyBom);
    }
    function apply(rows){
      var n = 0, nb = 0;
      rows.forEach(function(r){
        var d = r.dataset;
        var ok = (!onlyBom || d.bom) && (!capMin || parseFloat(d.cap || 0) >= capMin);
        r.classList.toggle('hide', !ok);
        if (ok){ n++; if (d.bom) nb++; }
      });
      return [n, nb];
    }
    var f = apply(rowsF), uu = apply(rowsU);
    var e;
    if ((e = document.getElementById('tlNFiled'))) e.textContent = f[0];
    if ((e = document.getElementById('tlNBom'))) e.textContent = f[1];
    if ((e = document.getElementById('tlNUnfiled'))) e.textContent = uu[0];
    if ((e = document.getElementById('tlUnfShown'))) e.textContent = uu[0];
  }
  bom.addEventListener('change', run);
  if (cap) cap.addEventListener('change', run);
  run();
}
// heatmap tabs: swap the grid in place, and swap the footnotes with it
function wireHeatTabs(){
  var tabs = document.querySelectorAll('.tabs .tab');
  if (!tabs.length) return;
  tabs.forEach(function(btn){
    btn.addEventListener('click', function(){
      var want = btn.dataset.pane;
      tabs.forEach(function(b){ b.classList.toggle('on', b === btn); });
      document.querySelectorAll('.hmpane, .hmfn').forEach(function(p){
        p.hidden = (p.dataset.pane !== want);
      });
    });
  });
}
"""


def render(rows, prows, ref_ym, pending, stats, meta) -> str:
    it_total = len(rows)
    tot_rev = sum(r["rev_now"] for r in rows if r["rev_now"] is not None)
    with_yoy = sum(1 for r in rows if r["yoy"] is not None)
    built = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    n_bom = len(bom_groups.all_codes())
    head = "\n".join([
        f'<div class="topline">AI서버 부품군 <b>{n_bom}종목</b> · '
        f'<b>{len(bom_groups.GROUPS)}그룹</b> · 기준월 <b>{esc(ref_ym)}</b> · '
        f'갱신 {esc(built)} KST</div>',
        f'<h1>대만 IT 월매출 by CB'
        + (f'<img class="cbmark" src="{LOGO_DATA_URI}" alt="">'
           if LOGO_DATA_URI else '<span class="cbmark-e">&#128526;</span>')
        + '</h1>',
        f'<p class="sub">기준월 <b style="color:var(--a1)">{esc(ref_ym)}</b> '
        f'&nbsp;·&nbsp; {it_total}개 종목 &nbsp;·&nbsp; 금액 단위 <b>백만 NTD</b></p>',
        '<div class="cards">',
        # honest label: consolidated filings mean a parent's revenue already
        # contains its listed subsidiaries', and we cannot know every such pair
        f'<div class="card"><div class="k">IT 합계 매출 '
        f'<span style="font-size:15px">(단순합·모자 이중계상 포함)</span></div>'
        f'<div class="v">{mn(tot_rev)}</div></div>',
        f'<div class="card"><div class="k">종목수</div><div class="v">{it_total}</div></div>',
        f'<div class="card"><div class="k">YoY 산출 가능</div><div class="v">{with_yoy}</div></div>',
        f'<div class="card"><div class="k">데이터 범위</div>'
        f'<div class="v sm">{esc(meta["lo"])} ~ {esc(ref_ym)}</div></div>',
        f'<div class="card"><div class="k">자료 생성일</div>'
        f'<div class="v sm">{esc(meta["pub"])}</div></div>',
        '</div>',
    ])

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- GitHub Pages는 같은 URL에 새 파일을 덮어쓰므로, 캐시가 남으면 어제 수치를
     보고 있으면서 오늘 것으로 착각하게 된다. 매번 새로 받도록 강제한다. -->
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>대만 IT 월매출 by CB {esc(ref_ym)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=42dot+Sans:wght@300..800&display=swap"
      rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{head}
{render_timeline(rows, meta["pend"], ref_ym, meta["mc"], sec=1)}
{render_charts(rows, ref_ym, meta["n_months"], stats, sec=2)}
{render_heatmap(rows, ref_ym, meta["heat_months"], sec=3)}
{render_inflection(rows, ref_ym, sec=4)}
{render_pending(pending, prows, ref_ym, it_total, n=5)}
{render_table(rows, ref_ym, n=6, clickable=meta["table_clickable"], pend=meta["pend"], mc=meta["mc"])}
{render_movers(rows, ref_ym, sec=7)}
{MODAL_HTML}
<footer>
출처: TWSE t187ap05_L / TPEx mopsfin_t187ap05_O / MOPS t21sc03
(원본 단위: 천 NTD &rarr; 화면 표시 백만 NTD)<br>
YoY·MoM·누계YoY는 build 시점에 원본 절대금액에서 매번 재계산합니다(DB 미저장).
전년동월 자료가 없으면 0이 아닌 빈칸으로 둡니다.
누계YoY = 회사 신고 누계영업수익(당월) ÷ 누계영업수익(전년 동월) - 1.<br>
한글명 3층: names.py(수동) &rarr; names_auto.py(한자 음독) &rarr; 한자명.
종목명에 마우스를 올리면 한자 원명이 나옵니다.<br>
생성 {esc(built)} &nbsp;·&nbsp; 차트 대상은 groups.py에서 관리
</footer>
</div>
<script>
{meta["payload"]}
{JS}
{MODAL_JS.replace("__LBL_H__", str(LBL_H)).replace("__LBL_FS__", str(LBL_FS)).replace("__LBL_ZIG__", str(LBL_ZIG))}
wire('tAll'); wire('tPend');
wire('tTL');
wire('tTLU');
wireModal(); wireMomToggle(); wireHeatTabs(); wireTimeline(1.0);
wireNumToggle({meta["n_months"]});
wireFilters({{table:'tAll', q:'q', ind:'fInd', mkt:'fMkt', bom:'fBom',
             small:'fSmall', bomOnly:'fBomOnly', unfiled:'fUnfiled',
             cap:'fCap', capFloorJo:1.0, count:'cnt',
             floor:{groups.MIN_REV_FOR_MOVERS_K}}});
markSorted('tAll', 5, 'desc');   // 당월매출 내림차순 = Python이 넘긴 기본 순서
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build dashboard.html from data.db")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--label-mode", choices=("js", "svg"), default="js",
                    help="월별 숫자 라벨 생성 방식. js=클릭 시 계산(기본), "
                         "svg=미리 렌더링")
    ap.add_argument("--modal-all", action="store_true",
                    help="ship the detail-modal series for every row in the table, "
                         "not just the BoM stocks, and make table rows clickable")
    args = ap.parse_args(argv)
    global LABEL_MODE
    LABEL_MODE = args.label_mode

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    ref_ym, pending = detect_months(conn)
    lo = conn.execute("SELECT MIN(ym) FROM revenue").fetchone()[0]
    pub = conn.execute("SELECT MAX(published_at) FROM revenue").fetchone()[0]
    have_months = conn.execute("SELECT COUNT(*) FROM (SELECT DISTINCT ym FROM revenue "
                               "WHERE ym <= ?)", (ref_ym,)).fetchone()[0]
    # charts show a fixed window; the extra history behind it exists so the
    # window's first month has its own prior year for YoY
    n_months = min(groups.CHART_MONTHS, have_months)
    heat_months = min(groups.HEATMAP_MONTHS, have_months)

    print(f"build.py  db={args.db}")
    print(f"  reference (complete) month : {ref_ym}")
    print(f"  in-progress month(s)       : {pending or 'none'}")
    print(f"  months of history          : {have_months}  ({lo} .. {ref_ym})")
    print(f"  chart window               : {n_months} months "
          f"({shift_ym(ref_ym, -(n_months - 1))} .. {ref_ym})")
    print(f"  IT industries              : {len(groups.IT_INDUSTRIES)}")

    mc = load_mktcap(conn)
    if mc["n"]:
        print(f"  market cap                 : {mc['n']:,}종목, "
              f"종가 기준일 {mc['date']}, USDTWD={mc['usdtwd']:,.4f} "
              f"USDKRW={mc['usdkrw']:,.2f}")
    else:
        print("  market cap                 : 재료 없음 (종가/주식수/환율 미수집)")

    master = load_master(conn)
    if not master:
        print("  !! company master is empty -- run `python fetch.py --master` for "
              "English abbreviations. Falling back to Chinese names only.")
    else:
        print(f"  company master             : {len(master)} codes with English abbr")

    rows, stats = load(conn, ref_ym, master, mc["cap"])
    prows = load_pending(conn, pending, master)
    named = sum(1 for r in rows if r.get("name_en"))
    layers = {}
    for r in rows:
        layers[r["name_layer"]] = layers.get(r["name_layer"], 0) + 1
    print(f"  IT companies               : {len(rows)}  ({named} with English abbr)")
    print(f"  Korean names               : manual={layers.get('manual', 0)} "
          f"auto={layers.get('auto', 0)} chinese-fallback={layers.get('zh', 0)}")
    blanks = [c for c in NAME_KR if not (NAME_KR.get(c) or '').strip()]
    if blanks:
        print(f"  names.py entries left blank: {len(blanks)} -> {blanks}")
    print(f"  filed for in-progress month: {len(prows)}")

    print("  metric coverage:")
    print(f"    YoY blank {stats['yoy_blank']:>4}  (prior-year taken from the source's "
          f"去年當月營收 for {stats['yoy_fallback']} months)")
    print(f"    MoM blank {stats['mom_blank']:>4}  (source fallback used "
          f"{stats['mom_fallback']})")
    print(f"    YoY가속 blank {stats['accel_blank']:>4}  (needs YoY in both "
          f"{ref_ym} and {shift_ym(ref_ym, -1)})")
    print(f"    누계YoY blank {stats['cum_blank']:>4}  "
          f"(of which no prior-year 累計 at all: {stats['cum_no_prior']})")
    print("  누계YoY basis = filed 累計營業收入 at ref vs the same month a year back.")
    print(f"    that prior-year 累計 vs the 去年累計營收 this row reports:")
    print(f"      identical {stats['cum_ly_exact']:>4}   "
          f"+/-6 rounding {stats['cum_ly_rounding']:>4}   "
          f"differ {stats['cum_ly_differ']:>4}")
    for code, name, ours, rep, d in stats["cum_ly_rows"][:6]:
        rel = d / rep * 100 if rep else float("nan")
        print(f"        {code} {name}: stored 2025 累計 {ours:,} vs reported "
              f"去年累計 {rep:,} ({rel:+.2f}%)")

    bom_all = bom_groups.all_codes()
    have = {r["code"] for r in rows}
    print(f"  BoM groups                 : {len(bom_groups.GROUPS)} groups, "
          f"{len(bom_all)} codes, {len(set(bom_all) & have)} present at {ref_ym}")
    outside = [c for c in bom_all if c in have and not next(
        r for r in rows if r["code"] == c)["in_it"]]
    print(f"  BoM codes bypassing the IT filter: {outside or 'none'}")
    prob = bom_groups.check_stages()
    print(f"  heatmap                    : {len(bom_groups.STAGES)} stages x "
          f"{heat_months} months"
          + (f"   !! STAGES problems: {prob}" if prob else "   (STAGES ok)"))
    bench = bom_groups.benchmark_members()
    print(f"  benchmark row              : {len(bench)} of {len(bom_all)} codes "
          f"(모자 제외 {len(bom_all) - len(bench)})")

    all_codes = [r["code"] for r in rows]
    pend_ym = pending[0][0] if pending else ""
    pend = None
    if pend_ym:
        p_rev, p_cum = load_pending_values(conn, pend_ym, all_codes)
        p_dates, p_stamps = load_disclosure(conn, pend_ym)
        dl, rolled = filing_deadline(pend_ym)
        pend = {"ym": pend_ym, "vals": p_rev, "cum": p_cum, "dates": p_dates,
                "stamps": p_stamps, "deadline": dl, "rolled": rolled}
        dated = sum(1 for c in p_rev if p_dates.get(c))
        print(f"  in-progress {pend_ym}        : {len(p_rev)} filed of {len(rows)}"
              f"  ({dated} with a recorded 발표일)  마감 {dl}"
              + ("  (주말 순연)" if rolled else ""))
    else:
        p_rev = p_cum = {}

    universe = all_codes if args.modal_all else bom_groups.all_codes()
    disc_all = load_disclosure_all(conn, universe)
    payload, n_modal = modal_payload(
        rows, universe, lo, ref_ym, disc=disc_all, pend_ym=pend_ym,
        deadline=(pend["deadline"] if pend else ""),
        pend_rev=p_rev, pend_cum=p_cum)
    print(f"  modal payload              : {n_modal} stocks "
          f"({'all rows' if args.modal_all else 'BoM only'}), "
          f"{len(payload.encode('utf-8')):,} bytes of JSON, "
          f"발표일 {sum(len(v) for v in disc_all.values())}건")

    html_out = render(rows, prows, ref_ym, pending, stats,
                      {"lo": lo, "pub": pub, "n_months": n_months,
                       "heat_months": heat_months, "payload": payload,
                       "table_clickable": args.modal_all, "pend": pend,
                       "mc": mc})
    print(f"  charts                     : {stats.get('chart_stocks', 0)} stocks "
          f"x {n_months} months = {stats.get('chart_points', 0)} points")
    print(f"    YoY from the source's 去年當月營收: {stats.get('chart_yoy_fallback', 0)}"
          f"   left blank: {stats.get('chart_yoy_blank', 0)}")
    if stats.get("chart_missing"):
        print(f"    !! bom_groups codes with no {ref_ym} data: "
              f"{stats['chart_missing']}")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    size = os.path.getsize(args.out)
    print(f"\nwrote {args.out}  {size:,} bytes ({size / 1024:.0f} KB, "
          f"{size / 1048576:.2f} MB)")
    if size > 5 * 1024 * 1024:
        print("  !! over the 5 MB ceiling")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
