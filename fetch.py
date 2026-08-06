#!/usr/bin/env python3
"""fetch.py -- Taiwan TWSE (上市) / TPEx (上櫃) monthly revenue -> SQLite.

Sources
-------
current month (JSON, refreshed intraday):
    https://openapi.twse.com.tw/v1/opendata/t187ap05_L   上市公司每月營業收入彙總表
    https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O  上櫃公司每月營業收入彙總表
history (Big5 HTML, one page per board/month/nationality):
    https://mopsov.twse.com.tw/nas/t21/{sii|otc}/t21sc03_{ROC}_{M}_{0|1}.html
      sii = 上市 (TWSE), otc = 上櫃 (TPEx)
      suffix 0 = domestic, 1 = KY (foreign-registered)

UNITS: every source reports 千元 (thousand NTD). Values are stored exactly as
published, in thousands. Column names carry a _k suffix so nothing converts
twice. Do not scale on read.

Only figures published by the source are stored. Percentages / YoY / MoM are
NOT stored -- build.py recomputes them on every run.

Usage
-----
    python fetch.py --backfill            # 2023-07 .. latest, then current-month APIs
    python fetch.py                       # daily: last 3 months + current-month APIs
    python fetch.py --from 2024-01 --to 2024-06
    python fetch.py --backfill --no-cache # ignore raw_cache/, re-download everything
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data.db")
CACHE_DIR = os.path.join(HERE, "raw_cache")

BACKFILL_START = (2022, 7)          # 12 months before the dashboard's 36-month
                                    # window, so month one of the window has its
                                    # own prior year and never needs the source's
                                    # 去年當月營收 as a stand-in
REFRESH_MONTHS = 3                  # daily mode re-pulls this many recent months
CACHE_EXEMPT_MONTHS = 3             # newest N months are never served from cache

MOPS_URL = "https://mopsov.twse.com.tw/nas/t21/{board}/t21sc03_{roc}_{m}_{suf}.html"
TWSE_API = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_API = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"

# Company master (--master). Changes rarely, so a monthly refresh is plenty.
# The two feeds are the same dataset with different field naming: TWSE uses
# Chinese keys, TPEx romanised ones, and TPEx's 英文簡稱 is called "Symbol".
TWSE_MASTER = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_MASTER = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
MASTER_FIELDS = {
    # market: (url, code_key, zh_name_key, en_name_key)
    "TWSE": (TWSE_MASTER, "公司代號", "公司簡稱", "英文簡稱"),
    "TPEX": (TPEX_MASTER, "SecuritiesCompanyCode", "CompanyAbbreviation", "Symbol"),
}

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tw-revenue/1.0",
    "Accept-Encoding": "gzip",
}

# MOPS Big5 pages are authoritative and carry a definitive domestic/KY flag,
# so they win over the JSON APIs when both describe the same (code, ym).
SOURCE_PRIORITY = {"mops": 3, "twse-api": 2, "tpex-api": 2}

# 발표일(disclosure)을 기록할지. 일상 갱신에서만 켠다 -- main()이 정한다.
# 백필이나 명시적 기간 지정은 이력 적재이므로 관측 시각에 의미가 없다.
RECORD_DISCLOSURE = True

RE_INDUSTRY = re.compile(r"產業別[:：]\s*(.+)")
RE_CODE = re.compile(r"^[0-9]{4,6}$")
RE_PUBDATE = re.compile(r"出表日期[:：]\s*(\d{2,3})/(\d{1,2})/(\d{1,2})")


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #
def make_ssl_context(insecure: bool = False) -> ssl.SSLContext:
    """Verified TLS context that can actually reach the TWSE hosts.

    twse.com.tw chains up to "TWCA Global Root CA", a legacy root that predates
    RFC 5280's Subject Key Identifier requirement. Python 3.13+ turns on
    VERIFY_X509_STRICT by default, which rejects it outright
    ("Missing Subject Key Identifier") even though Windows itself trusts the
    chain. We keep full chain + hostname verification and drop only that strict
    flag, then merge in the Windows trust store so the TWCA roots are present.
    """
    if insecure:
        print("!! TLS verification DISABLED (--insecure)", file=sys.stderr)
        return ssl._create_unverified_context()

    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

    if sys.platform == "win32":
        pems = []
        for store in ("ROOT", "CA"):
            try:
                certs = ssl.enum_certificates(store)
            except Exception:
                continue
            for der, enc, trust in certs:
                if enc != "x509_asn":
                    continue
                if isinstance(trust, set) and "1.3.6.1.5.5.7.3.1" not in trust:
                    continue  # not valid for TLS server auth
                try:
                    pems.append(ssl.DER_cert_to_PEM_cert(der))
                except Exception:
                    pass
        if pems:
            try:
                ctx.load_verify_locations(cadata="".join(pems))
            except ssl.SSLError:
                pass
    return ctx


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Http:
    def __init__(self, ctx: ssl.SSLContext, sleep: float = 0.6, retries: int = 3):
        self.ctx = ctx
        self.sleep = sleep
        self.retries = retries
        self._last = 0.0

    def get(self, url: str, timeout: int = 90) -> bytes:
        last_err = None
        for attempt in range(1, self.retries + 1):
            wait = self.sleep - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return body
            except urllib.error.HTTPError as e:
                if e.code in (404, 403):
                    raise
                last_err = e
            except ssl.SSLCertVerificationError as e:
                raise RuntimeError(
                    f"TLS verification failed for {url}: {e}\n"
                    "The TWCA root may be missing from the Windows trust store. "
                    "Open https://openapi.twse.com.tw/ in Edge once to let Windows "
                    "fetch it, or re-run with --insecure if you accept the risk."
                ) from e
            except Exception as e:
                last_err = e
            finally:
                self._last = time.monotonic()
            if attempt < self.retries:
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"GET failed after {self.retries} tries: {url} ({last_err!r})")


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #
def roc_year(greg_year: int) -> int:
    return greg_year - 1911


def parse_roc_ym(s: str) -> str | None:
    """'11506' -> '2026-06'. Returns None if unparseable."""
    s = (s or "").strip()
    if not s.isdigit() or len(s) not in (5, 6):
        return None
    if len(s) == 5:
        roc, mm = int(s[:3]), int(s[3:])
    else:
        roc, mm = int(s[:4]), int(s[4:])
    if not 1 <= mm <= 12:
        return None
    return f"{roc + 1911:04d}-{mm:02d}"


def parse_roc_date(s: str) -> str | None:
    """出表日期 '1150717' or '115/08/05' -> '2026-08-05'. None if unparseable.

    This is the source's own compilation date and it is what decides freshness:
    MOPS rebuilds its HTML pages nightly, while the JSON APIs can lag by weeks,
    so a 정정공시 (restatement) is recognised by a newer 出表日期.
    """
    s = (s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{2,3})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        roc, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif s.isdigit() and len(s) in (6, 7):
        cut = len(s) - 4
        roc, mm, dd = int(s[:cut]), int(s[cut:cut + 2]), int(s[cut + 2:])
    else:
        return None
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    return f"{roc + 1911:04d}-{mm:02d}-{dd:02d}"


def prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def month_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], start
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def latest_published_month(today: date | None = None) -> tuple[int, int]:
    """Revenue for month M is filed by the 10th of M+1, so the newest month
    that can have any data is the previous calendar month."""
    t = today or date.today()
    return prev_month(t.year, t.month)


def parse_ym_arg(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {s!r}")
    y, mm = int(m.group(1)), int(m.group(2))
    if not 1 <= mm <= 12:
        raise argparse.ArgumentTypeError(f"bad month in {s!r}")
    return (y, mm)


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #
def to_int_k(s) -> int | None:
    """Parse a published thousand-NTD figure. Blank / '-' / '&nbsp;' -> None."""
    if s is None:
        return None
    t = str(s).replace(",", "").replace("\xa0", " ").strip()
    if t in ("", "-", "--", "N/A", "n/a"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1].strip()
    try:
        v = int(round(float(t)))
    except ValueError:
        return None
    return -v if neg else v


# --------------------------------------------------------------------------- #
# MOPS Big5 HTML parsing
# --------------------------------------------------------------------------- #
class MopsParser(HTMLParser):
    """Collect (industry, [(tag, text), ...]) for every table row.

    The pages nest a data table inside an industry-header table, use mixed-case
    tags (<Td>), and leave the final <tr> unclosed. Rows are therefore flushed
    on </tr> *and* </table>, and industry is picked up from any cell matching
    '產業別：...'.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str | None, list[tuple[str, str]]]] = []
        self.industry: str | None = None
        self._row: list[tuple[str, str]] | None = None
        self._cell: list[str] | None = None
        self._tag: str | None = None

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "tr":
            self._row = []
        elif t in ("td", "th"):
            if self._row is None:
                self._row = []
            self._cell, self._tag = [], t
        elif t == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._row.append((self._tag or t, "".join(self._cell).strip()))
            self._cell = self._tag = None
        elif t in ("tr", "table"):
            if self._row:
                self._flush(self._row)
            self._row = None
            if t == "table":
                self._cell = self._tag = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def _flush(self, row):
        for _, text in row:
            m = RE_INDUSTRY.search(text)
            if m:
                self.industry = m.group(1).strip()
                return          # header row, not data
        self.rows.append((self.industry, row))


def decode_mops(raw: bytes) -> str:
    """Decode a MOPS page.

    The pages declare charset=big5, but they contain characters outside the
    strict Big5 set -- 碁 in 宏碁資訊 / 安碁 / 立碁 / 洛碁, for one. Decoding as
    'big5' does not merely drop that character: it desynchronises the two-byte
    stream and garbles the rest of the name (恒耀國際 -> '?矬ㄟ篕?'). cp950 is
    Microsoft's Big5 superset and decodes every observed page losslessly, with
    big5hkscs as a second chance before we accept lossy output.
    """
    for enc in ("cp950", "big5hkscs"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp950", errors="replace")


def parse_mops_page(html: str, ym: str, board: str, is_ky: int) -> list[dict]:
    """Extract company rows. Columns, in order:
    公司代號 公司名稱 當月營收 上月營收 去年當月營收 上月比較增減(%)
    去年同月增減(%) 當月累計營收 去年累計營收 前期比較增減(%) [備註]

    The two '增減(%)' columns are deliberately dropped -- build.py recomputes.
    """
    p = MopsParser()
    p.feed(html)
    market = "TWSE" if board == "sii" else "TPEX"
    pm = RE_PUBDATE.search(html)
    published_at = parse_roc_date("/".join(pm.groups())) if pm else None
    out = []
    for industry, row in p.rows:
        if len(row) < 10:
            continue
        # 合計 / subtotal rows put a <th> in the first column.
        if row[0][0] != "td":
            continue
        code = row[0][1].strip()
        if not RE_CODE.fullmatch(code):
            continue
        cells = [c[1] for c in row]
        out.append({
            "code": code,
            "ym": ym,
            "name": cells[1].strip() or None,
            "industry": industry,
            "market": market,
            "is_ky": is_ky,
            "rev_month_k": to_int_k(cells[2]),
            "rev_last_month_k": to_int_k(cells[3]),
            "rev_ly_month_k": to_int_k(cells[4]),
            "rev_cum_k": to_int_k(cells[7]),
            "rev_cum_ly_k": to_int_k(cells[8]),
            "note": (cells[10].strip() if len(cells) > 10 else None) or None,
            "published_at": published_at,
            "source": f"mops:{board}_{1 if is_ky else 0}",
        })
    return out


def fetch_mops_month(http: Http, y: int, m: int, use_cache: bool,
                     newest: tuple[int, int]) -> list[dict]:
    ym = f"{y:04d}-{m:02d}"
    rows: list[dict] = []
    fresh_window = month_range(
        # newest CACHE_EXEMPT_MONTHS months must always be re-downloaded
        # (companies file late and issue 정정공시 corrections)
        _shift(newest, -(CACHE_EXEMPT_MONTHS - 1)), newest)
    cacheable = use_cache and (y, m) not in fresh_window

    for board in ("sii", "otc"):
        for suf in (0, 1):
            url = MOPS_URL.format(board=board, roc=roc_year(y), m=m, suf=suf)
            cache_fp = os.path.join(CACHE_DIR, f"{board}_{roc_year(y)}_{m}_{suf}.html")
            raw = None
            if cacheable and os.path.isfile(cache_fp) and os.path.getsize(cache_fp) > 0:
                with open(cache_fp, "rb") as fh:
                    raw = fh.read()
            if raw is None:
                try:
                    raw = http.get(url)
                except urllib.error.HTTPError as e:
                    print(f"    {ym} {board}_{suf}: HTTP {e.code}, skipped")
                    continue
                if cacheable:
                    os.makedirs(CACHE_DIR, exist_ok=True)
                    with open(cache_fp, "wb") as fh:
                        fh.write(raw)
            html = decode_mops(raw)
            got = parse_mops_page(html, ym, board, suf)
            rows.extend(got)
            print(f"    {ym} {board}_{suf}: {len(got):>4} rows ({len(raw):>7}B)")
    return rows


def _shift(ym: tuple[int, int], delta: int) -> tuple[int, int]:
    y, m = ym
    total = y * 12 + (m - 1) + delta
    return (total // 12, total % 12 + 1)


# --------------------------------------------------------------------------- #
# JSON APIs (current month)
# --------------------------------------------------------------------------- #
def parse_api_rows(data: list[dict], market: str, source: str) -> list[dict]:
    out = []
    for d in data:
        code = (d.get("公司代號") or "").strip()
        ym = parse_roc_ym(d.get("資料年月", ""))
        if not RE_CODE.fullmatch(code) or not ym:
            continue
        out.append({
            "code": code,
            "ym": ym,
            "name": (d.get("公司名稱") or "").strip() or None,
            "industry": (d.get("產業別") or "").strip() or None,
            "market": market,
            # KY status is only reliable from the MOPS _0/_1 split; leave it
            # unknown here so a MOPS row can fill it in without being clobbered.
            "is_ky": None,
            "rev_month_k": to_int_k(d.get("營業收入-當月營收")),
            "rev_last_month_k": to_int_k(d.get("營業收入-上月營收")),
            "rev_ly_month_k": to_int_k(d.get("營業收入-去年當月營收")),
            "rev_cum_k": to_int_k(d.get("累計營業收入-當月累計營收")),
            "rev_cum_ly_k": to_int_k(d.get("累計營業收入-去年累計營收")),
            "note": (d.get("備註") or "").strip() or None,
            "published_at": parse_roc_date(d.get("出表日期", "")),
            "source": source,
        })
    return out


def clean(s) -> str | None:
    """Trim and collapse whitespace, including U+3000.

    The TPEx master pads values with ideographic spaces ("WIN　　",
    "MORNSUN　"), which survive a naive comparison and render as a visible
    gap after the name.
    """
    if s is None:
        return None
    t = re.sub(r"[\s　]+", " ", str(s)).strip()
    return t or None


def fetch_master(http: Http) -> list[dict]:
    """Company master: code, Chinese short name, English abbreviation, market."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for market, (url, k_code, k_zh, k_en) in MASTER_FIELDS.items():
        try:
            data = json.loads(http.get(url).decode("utf-8-sig"))
        except Exception as e:
            print(f"    {market} master: FAILED {type(e).__name__}: {str(e)[:120]}")
            continue
        if not isinstance(data, list) or not data:
            print(f"    {market} master: unexpected payload, skipped")
            continue
        if k_en not in data[0]:
            print(f"    {market} master: field {k_en!r} is gone -- schema changed, "
                  f"keys are {list(data[0])[:8]}...")
            continue
        got = 0
        for d in data:
            code = clean(d.get(k_code))
            if not code or not RE_CODE.fullmatch(code):
                continue
            rows.append({"code": code, "name_zh": clean(d.get(k_zh)),
                         "name_en": clean(d.get(k_en)), "market": market,
                         "source": url.rsplit("/", 1)[-1], "fetched_at": now})
            got += 1
        n_en = sum(1 for r in rows[-got:] if r["name_en"])
        print(f"    {market} master: {got:>5} rows, {n_en} with English abbr")
    return rows


def fetch_apis(http: Http) -> list[dict]:
    rows = []
    for url, market, src in ((TWSE_API, "TWSE", "twse-api"), (TPEX_API, "TPEX", "tpex-api")):
        try:
            data = json.loads(http.get(url).decode("utf-8-sig"))
        except Exception as e:
            print(f"    {src}: FAILED {type(e).__name__}: {str(e)[:120]}")
            continue
        if not isinstance(data, list):
            print(f"    {src}: unexpected payload (not a list), skipped")
            continue
        got = parse_api_rows(data, market, src)
        months = sorted({r["ym"] for r in got})
        print(f"    {src}: {len(got):>4} rows, months={months}")
        rows.extend(got)
    return rows


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue (
    code             TEXT NOT NULL,          -- 公司代號
    ym               TEXT NOT NULL,          -- 'YYYY-MM' (Gregorian)
    name             TEXT,
    industry         TEXT,
    market           TEXT,                   -- 'TWSE' (上市) | 'TPEX' (上櫃)
    is_ky            INTEGER,                -- 1 = KY/foreign, 0 = domestic, NULL = unknown
    rev_month_k      INTEGER,                -- 當月營收        [thousand NTD]
    rev_last_month_k INTEGER,                -- 上月營收        [thousand NTD]
    rev_ly_month_k   INTEGER,                -- 去年當月營收    [thousand NTD]
    rev_cum_k        INTEGER,                -- 當月累計營收    [thousand NTD]
    rev_cum_ly_k     INTEGER,                -- 去年累計營收    [thousand NTD]
    note             TEXT,                   -- 備註
    published_at     TEXT,                   -- 出表日期: when the source compiled this
    source           TEXT,
    fetched_at       TEXT,
    PRIMARY KEY (code, ym)
);
CREATE INDEX IF NOT EXISTS idx_revenue_ym   ON revenue(ym);
CREATE INDEX IF NOT EXISTS idx_revenue_code ON revenue(code);

-- audit trail for 정정공시 (restatements): whenever a stored figure changes
CREATE TABLE IF NOT EXISTS revision_log (
    code TEXT, ym TEXT, field TEXT,
    old_value INTEGER, new_value INTEGER,
    source TEXT, changed_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- 발표일 추적. (code, ym)이 DB에 처음 들어온 순간을 기록한다.
--
-- INSERT OR IGNORE 로만 쓴다. 정정공시로 금액이 바뀌어도 최초 발표일은
-- 그대로여야 하므로 절대 덮어쓰지 않는다.
--
-- 이 테이블이 생기기 전에 이미 적재된 행에는 항목이 없다(= 발표일 미상).
-- 出表日期로 소급하지 않는다: 그건 MOPS가 페이지를 만든 날이지 회사가 실제로
-- 공시한 날이 아니라서, 소급하면 전 종목이 같은 날짜가 되는 가짜 데이터가 된다.
--
-- 향후: 종목별 first_seen 일자 중앙값 -> "보통 N일경 발표" 예측
CREATE TABLE IF NOT EXISTS disclosure (
    code            TEXT NOT NULL,
    ym              TEXT NOT NULL,          -- 'YYYY-MM' (Gregorian)
    first_seen_date TEXT,                   -- 'YYYY-MM-DD' local
    first_seen_ts   TEXT,                   -- ISO8601 local with offset
    source          TEXT,                   -- 'api' | 'mops'
    PRIMARY KEY (code, ym)
);
CREATE INDEX IF NOT EXISTS idx_disclosure_ym ON disclosure(ym);

-- company master, refreshed with --master (monthly is plenty)
CREATE TABLE IF NOT EXISTS company (
    code       TEXT PRIMARY KEY,
    name_zh    TEXT,                        -- 公司簡稱 / CompanyAbbreviation
    name_en    TEXT,                        -- 英文簡稱 / Symbol
    market     TEXT,                        -- 'TWSE' | 'TPEX'
    source     TEXT,
    fetched_at TEXT
);
"""

COMPANY_UPSERT = """
INSERT INTO company (code, name_zh, name_en, market, source, fetched_at)
VALUES (:code, :name_zh, :name_en, :market, :source, :fetched_at)
ON CONFLICT(code) DO UPDATE SET
    name_zh    = COALESCE(excluded.name_zh, company.name_zh),
    name_en    = COALESCE(excluded.name_en, company.name_en),
    market     = COALESCE(excluded.market, company.market),
    source     = excluded.source,
    fetched_at = excluded.fetched_at
"""

VALUE_FIELDS = ("rev_month_k", "rev_last_month_k", "rev_ly_month_k",
                "rev_cum_k", "rev_cum_ly_k")

COLUMNS = ("code", "ym", "name", "industry", "market", "is_ky",
           "rev_month_k", "rev_last_month_k", "rev_ly_month_k",
           "rev_cum_k", "rev_cum_ly_k", "note", "published_at",
           "source", "fetched_at")

UPSERT = f"""
INSERT INTO revenue ({','.join(COLUMNS)})
VALUES ({','.join('?' * len(COLUMNS))})
ON CONFLICT(code, ym) DO UPDATE SET
    name             = COALESCE(excluded.name, revenue.name),
    industry         = COALESCE(excluded.industry, revenue.industry),
    market           = COALESCE(excluded.market, revenue.market),
    is_ky            = COALESCE(excluded.is_ky, revenue.is_ky),
    rev_month_k      = excluded.rev_month_k,
    rev_last_month_k = excluded.rev_last_month_k,
    rev_ly_month_k   = excluded.rev_ly_month_k,
    rev_cum_k        = excluded.rev_cum_k,
    rev_cum_ly_k     = excluded.rev_cum_ly_k,
    note             = excluded.note,
    published_at     = excluded.published_at,
    source           = excluded.source,
    fetched_at       = excluded.fetched_at
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(revenue)")}
    for col, decl in (("published_at", "TEXT"),):
        if col not in have:
            conn.execute(f"ALTER TABLE revenue ADD COLUMN {col} {decl}")
            print(f"  migrated: added revenue.{col}")
    return conn


def _src_priority(source: str) -> int:
    for prefix, pri in SOURCE_PRIORITY.items():
        if (source or "").startswith(prefix):
            return pri
    return 1


def _rank(r) -> tuple:
    """Freshness ordering, applied identically to incoming dicts and stored rows.
    A row wins if it actually carries a figure, then on the source's own 出表日期,
    then on source authority. Comparing 出表日期 is what stops a stale JSON
    snapshot from overwriting a MOPS page that already reflects a 정정공시."""
    return (r["rev_month_k"] is not None,
            r["published_at"] or "",
            _src_priority(r["source"]))


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """One row per (code, ym). The same company can surface on both the TWSE and
    TPEx feeds, so collapse by code and keep the freshest."""
    best: dict[tuple[str, str], dict] = {}
    dropped = 0
    for r in rows:
        key = (r["code"], r["ym"])
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        dropped += 1
        if _rank(r) > _rank(cur):
            best[key] = r
    return list(best.values()), dropped


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """Insert/update rows. Stale rows are skipped, changed figures are logged."""
    rows, dup_dropped = dedupe(rows)
    stats = {"rows": 0, "new": 0, "revised": 0, "stale": 0, "first_seen": 0,
             "dup_dropped": dup_dropped}
    if not rows:
        return stats

    existing: dict[tuple[str, str], sqlite3.Row] = {}
    cur = conn.cursor()
    keys = list({(r["code"], r["ym"]) for r in rows})
    cols = ",".join(VALUE_FIELDS)
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        q = " OR ".join(["(code=? AND ym=?)"] * len(chunk))
        params = [x for kv in chunk for x in kv]
        for row in cur.execute(
            f"SELECT code, ym, published_at, source, {cols} FROM revenue WHERE {q}", params
        ):
            existing[(row["code"], row["ym"])] = row

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    local = datetime.now().astimezone()
    revisions, writable, first_seen = [], [], []
    for r in rows:
        old = existing.get((r["code"], r["ym"]))
        if old is None:
            stats["new"] += 1
            writable.append(r)
            # "처음 들어왔다"가 "방금 발표됐다"를 뜻하는 것은 일상 폴링일 때뿐이다.
            # 백필은 과거 이력을 적재하는 작업이라 전 종목·전 개월이 한꺼번에
            # 신규로 잡히고, 그대로 기록하면 4년치가 전부 오늘 발표된 것으로
            # 둔갑한다 (빈 DB에서 백필한 서버에서 실제로 92,875건이 그렇게 됐다).
            if RECORD_DISCLOSURE:
                first_seen.append((r["code"], r["ym"],
                                   local.date().isoformat(),
                                   local.isoformat(timespec="seconds"),
                                   "mops" if (r["source"] or "").startswith("mops")
                                   else "api"))
            continue
        if _rank(r) < _rank(old):
            stats["stale"] += 1          # older snapshot than what we hold
            continue
        changed = [(f, old[f], r[f]) for f in VALUE_FIELDS
                   if r[f] is not None and old[f] != r[f]]
        if changed:
            stats["revised"] += 1
            revisions += [(r["code"], r["ym"], f, ov, nv, r["source"], now)
                          for f, ov, nv in changed]
        writable.append(r)

    payload = [tuple(r[c] if c != "fetched_at" else now for c in COLUMNS) for r in writable]
    with conn:
        conn.executemany(UPSERT, payload)
        if revisions:
            conn.executemany(
                "INSERT INTO revision_log (code, ym, field, old_value, new_value,"
                " source, changed_at) VALUES (?,?,?,?,?,?,?)", revisions)
        if first_seen:
            # OR IGNORE: a (code, ym) already on file keeps its original date,
            # whatever a later restatement does to the figures
            conn.executemany(
                "INSERT OR IGNORE INTO disclosure (code, ym, first_seen_date,"
                " first_seen_ts, source) VALUES (?,?,?,?,?)", first_seen)
    stats["rows"] = len(writable)
    stats["first_seen"] = len(first_seen)
    return stats


def upsert_company(conn: sqlite3.Connection, rows: list[dict]) -> dict:
    """One row per code. A code can appear in both masters (rare); the first
    market wins so an existing English name is never dropped for a blank."""
    seen: dict[str, dict] = {}
    dropped = 0
    for r in rows:
        cur = seen.get(r["code"])
        if cur is None:
            seen[r["code"]] = r
        else:
            dropped += 1
            if not cur["name_en"] and r["name_en"]:
                seen[r["code"]] = r
    before = {r["code"] for r in conn.execute("SELECT code FROM company")}
    with conn:
        conn.executemany(COMPANY_UPSERT, list(seen.values()))
    new = len(set(seen) - before)
    return {"rows": len(seen), "new": new, "dup_dropped": dropped}


def report_master(conn: sqlite3.Connection):
    c = conn.cursor()
    n, en = c.execute("SELECT COUNT(*), SUM(name_en IS NOT NULL) FROM company").fetchone()
    print("\n" + "=" * 68)
    print(f"company master: {n:,} codes, {en or 0:,} with an English abbreviation")
    for r in c.execute("SELECT market, COUNT(*) n, SUM(name_en IS NOT NULL) e "
                       "FROM company GROUP BY market ORDER BY market"):
        print(f"  {r['market']:<6} {r['n']:>6} codes  {r['e'] or 0:>6} with English")

    # how much of the revenue universe can now be labelled
    row = c.execute("""
        SELECT COUNT(*) n, SUM(m.name_en IS NOT NULL) e
        FROM (SELECT DISTINCT code FROM revenue) r
        LEFT JOIN company m ON m.code = r.code""").fetchone()
    # On a fresh database --master runs before any revenue exists, so this
    # denominator is legitimately zero. It never happened locally because
    # data.db always already had rows.
    n_uni, n_en = row["n"] or 0, row["e"] or 0
    pctstr = f"{n_en / n_uni * 100:.1f}%" if n_uni else "revenue 테이블이 아직 비어 있음"
    print(f"  revenue universe: {n_uni} codes, {n_en} matched to an "
          f"English abbr ({pctstr})")
    unmatched = [r["code"] for r in c.execute(
        "SELECT r.code FROM (SELECT DISTINCT code FROM revenue) r "
        "LEFT JOIN company m ON m.code=r.code WHERE m.name_en IS NULL LIMIT 12")]
    if unmatched:
        print(f"  without English abbr: {unmatched}")
    # sample the busiest month, not the newest -- the newest is still being filed
    print("\n  sample (largest by revenue in the most complete month):")
    for r in c.execute("SELECT c.code, c.name_zh, c.name_en, c.market FROM company c "
                       "JOIN revenue v ON v.code=c.code AND v.ym=("
                       "  SELECT ym FROM revenue GROUP BY ym"
                       "  ORDER BY COUNT(*) DESC, ym DESC LIMIT 1) "
                       "WHERE c.name_en IS NOT NULL ORDER BY v.rev_month_k DESC LIMIT 8"):
        print(f"    {r['code']}  {r['name_zh']}  {r['name_en']}  ({r['market']})")
    print("=" * 68)


def set_meta(conn: sqlite3.Connection, **kv):
    with conn:
        conn.executemany("INSERT INTO meta(k,v) VALUES(?,?) "
                         "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         [(k, str(v)) for k, v in kv.items()])


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def report(conn: sqlite3.Connection):
    c = conn.cursor()
    total, codes, months = c.execute(
        "SELECT COUNT(*), COUNT(DISTINCT code), COUNT(DISTINCT ym) FROM revenue").fetchone()
    print("\n" + "=" * 68)
    print(f"data.db  rows={total:,}  companies={codes:,}  months={months}")
    lo, hi = c.execute("SELECT MIN(ym), MAX(ym) FROM revenue").fetchone()
    print(f"coverage {lo} .. {hi}")

    print("\nby market / KY:")
    for row in c.execute(
        "SELECT market, COALESCE(is_ky,-1) k, COUNT(DISTINCT code) n FROM revenue "
        "GROUP BY market, k ORDER BY market, k"
    ):
        label = {0: "domestic", 1: "KY", -1: "unknown"}[row["k"]]
        print(f"  {row['market']:<5} {label:<9} {row['n']:>5} companies")

    print("\nrows per month (last 8):")
    for row in c.execute(
        "SELECT ym, COUNT(*) n, SUM(rev_month_k) s FROM revenue "
        "GROUP BY ym ORDER BY ym DESC LIMIT 8"
    ):
        s = f"{row['s']:,}" if row["s"] is not None else "-"
        print(f"  {row['ym']}  {row['n']:>5} rows   total {s} k-NTD")

    print("\nsource mix (which feed holds the winning row):")
    for row in c.execute("SELECT source, COUNT(*) n, MAX(published_at) p FROM revenue "
                         "GROUP BY source ORDER BY n DESC"):
        print(f"  {row['source']:<16} {row['n']:>7,}  newest 出表日期 {row['p']}")

    print("\ndisclosure (발표일) tracking:")
    tot = c.execute("SELECT COUNT(*) FROM disclosure").fetchone()[0]
    rev_tot = c.execute("SELECT COUNT(*) FROM revenue").fetchone()[0]
    print(f"  rows with a recorded first_seen: {tot:,} of {rev_tot:,} "
          f"(나머지는 이 기능 이전 적재분 -- 소급 불가)")
    for row in c.execute("SELECT ym, source, COUNT(*) n, MIN(first_seen_date) a, "
                         "MAX(first_seen_date) b FROM disclosure "
                         "GROUP BY ym, source ORDER BY ym DESC, source LIMIT 8"):
        span = row["a"] if row["a"] == row["b"] else f"{row['a']} ~ {row['b']}"
        print(f"    {row['ym']}  {row['source']:<5} {row['n']:>5} 종목   {span}")

    nrev = c.execute("SELECT COUNT(*) FROM revision_log").fetchone()[0]
    print(f"\nrestatements logged: {nrev}")
    for row in c.execute(
        "SELECT r.code, v.name, r.ym, r.field, r.old_value, r.new_value FROM revision_log r "
        "LEFT JOIN revenue v ON v.code=r.code AND v.ym=r.ym "
        "ORDER BY r.changed_at DESC, r.code LIMIT 6"
    ):
        ov = f"{row['old_value']:,}" if row["old_value"] is not None else "NULL"
        nv = f"{row['new_value']:,}" if row["new_value"] is not None else "NULL"
        print(f"  {row['code']} {(row['name'] or '?'):<11} {row['ym']} {row['field']:<16} "
              f"{ov:>14} -> {nv:>14}")

    print("\nsanity -- TSMC 2330, last 6 months (千元):")
    for row in c.execute(
        "SELECT ym, name, rev_month_k, rev_cum_k FROM revenue "
        "WHERE code='2330' ORDER BY ym DESC LIMIT 6"
    ):
        cum = f"{row['rev_cum_k']:,}" if row["rev_cum_k"] is not None else "-"
        print(f"  {row['ym']}  {row['name'] or '?'} {row['rev_month_k']:>15,}"
              f"   cum {cum:>16}")

    miss = c.execute("SELECT COUNT(*) FROM revenue WHERE rev_month_k IS NULL").fetchone()[0]
    bad = c.execute("SELECT COUNT(*) FROM revenue WHERE name LIKE '%'||char(65533)||'%'"
                    ).fetchone()[0]
    print(f"\nrows with NULL 當月營收: {miss}")
    print(f"names with undecodable characters: {bad}")
    print("=" * 68)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch TWSE/TPEx monthly revenue into SQLite.")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--backfill", action="store_true",
                    help=f"pull every month from {BACKFILL_START[0]}-{BACKFILL_START[1]:02d}")
    ap.add_argument("--from", dest="ym_from", type=parse_ym_arg)
    ap.add_argument("--to", dest="ym_to", type=parse_ym_arg)
    ap.add_argument("--master", action="store_true",
                    help="refresh the company master (code/中文名/English abbr/market) "
                         "and exit; revenue is left alone. Monthly is plenty.")
    ap.add_argument("--no-api", action="store_true", help="skip the current-month JSON APIs")
    ap.add_argument("--no-mops", action="store_true", help="skip the Big5 history pages")
    ap.add_argument("--no-cache", action="store_true", help="ignore raw_cache/")
    ap.add_argument("--sleep", type=float, default=0.6, help="seconds between requests")
    ap.add_argument("--insecure", action="store_true", help="disable TLS verification")
    args = ap.parse_args(argv)

    if args.master:
        print(f"fetch.py --master  db={args.db}")
        http = Http(make_ssl_context(args.insecure), sleep=args.sleep)
        conn = connect(args.db)
        rows = fetch_master(http)
        if not rows:
            print("no master rows fetched -- nothing written", file=sys.stderr)
            conn.close()
            return 1
        st = upsert_company(conn, rows)
        print(f"    -> stored={st['rows']} new={st['new']} dedup={st['dup_dropped']}")
        set_meta(conn, last_master_fetch=datetime.now(timezone.utc)
                 .isoformat(timespec="seconds"))
        report_master(conn)
        conn.close()
        return 0

    newest = latest_published_month()
    if args.ym_from or args.ym_to:
        start = args.ym_from or BACKFILL_START
        end = args.ym_to or newest
    elif args.backfill:
        start, end = BACKFILL_START, newest
    else:
        start, end = _shift(newest, -(REFRESH_MONTHS - 1)), newest
    if start > end:
        print(f"error: --from {start} is after --to {end}", file=sys.stderr)
        return 2

    # 발표일은 "진행 중인 달을 반복해서 들여다보다가 새로 뜬 것"에만 의미가 있다.
    global RECORD_DISCLOSURE
    RECORD_DISCLOSURE = not (args.backfill or args.ym_from or args.ym_to)

    months = month_range(start, end)
    print(f"fetch.py  db={args.db}")
    print(f"발표일 기록: {'예 (일상 갱신)' if RECORD_DISCLOSURE else '아니오 (이력 적재)'}")
    print(f"target months: {start[0]}-{start[1]:02d} .. {end[0]}-{end[1]:02d} "
          f"({len(months)} months)")

    http = Http(make_ssl_context(args.insecure), sleep=args.sleep)
    conn = connect(args.db)
    t0 = time.monotonic()
    agg = {"rows": 0, "new": 0, "revised": 0, "stale": 0, "first_seen": 0,
           "dup_dropped": 0}

    def merge(stats):
        for k in agg:
            agg[k] += stats[k]

    def fmt(st):
        return (f"stored={st['rows']} new={st['new']} revised={st['revised']} "
                f"stale={st['stale']} dedup={st['dup_dropped']} "
                f"first_seen={st['first_seen']}")

    if not args.no_mops:
        print(f"\n[1] MOPS Big5 history ({len(months) * 4} pages)")
        for y, m in months:
            rows = fetch_mops_month(http, y, m, use_cache=not args.no_cache, newest=newest)
            st = upsert(conn, rows)
            merge(st)
            print(f"    -> {ym_label(y, m)} {fmt(st)}")

    if not args.no_api:
        print("\n[2] current-month JSON APIs")
        rows = fetch_apis(http)
        st = upsert(conn, rows)
        merge(st)
        print(f"    -> {fmt(st)}")

    set_meta(conn,
             last_fetch=datetime.now(timezone.utc).isoformat(timespec="seconds"),
             last_range=f"{start[0]}-{start[1]:02d}..{end[0]}-{end[1]:02d}",
             unit="thousand NTD (千元) as published; do not rescale")

    print(f"\ntotals: {fmt(agg)}  in {time.monotonic() - t0:.1f}s")
    report(conn)
    conn.close()
    return 0


def ym_label(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


if __name__ == "__main__":
    sys.exit(main())
