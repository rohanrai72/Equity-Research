"""
Equity Research Backend
=======================
Flask app exposing the following routes:

    GET /                         service info
    GET /search?q=<query>         search NSE+BSE listed companies
    GET /quote/<symbol>           live NSE quote (existing)
    GET /news/<bsecode>           BSE corp announcements (existing)
    GET /price/<symbol>           historical OHLC via yfinance
    GET /fundamentals/<symbol>    income/balance/cashflow + ratios + shareholding
                                  (Screener.in scrape, 10+ years where available)

Environment variables:
    PORT          (Render sets this)
    PROXY_URL     (optional, e.g. http://user:pass@proxy:8080) — used for
                  outbound calls if Render's IP gets blocked by NSE/Yahoo

Deploy on Render free tier with `gunicorn app:app`.
"""

import csv
import io
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

# Yahoo Finance v8 chart endpoint — no auth required, used directly.
YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart"

# --------------------------------------------------------------------------- #
#  Setup
# --------------------------------------------------------------------------- #

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = app.logger

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
PROXY_URL = os.environ.get("PROXY_URL")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
DEFAULT_TIMEOUT = 20


def _common_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    h = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    if extra:
        h.update(extra)
    return h


# --------------------------------------------------------------------------- #
#  NSE session (cookie warmup required for api.nseindia.com)
# --------------------------------------------------------------------------- #

_nse_session: Optional[requests.Session] = None
_nse_session_ts: float = 0.0
_nse_lock = threading.Lock()


def nse_session() -> requests.Session:
    """Return a session with NSE cookies. Refreshes every 20 minutes."""
    global _nse_session, _nse_session_ts
    with _nse_lock:
        if _nse_session is None or time.time() - _nse_session_ts > 1200:
            s = requests.Session()
            s.headers.update(_common_headers({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.nseindia.com/",
            }))
            if PROXIES:
                s.proxies.update(PROXIES)
            try:
                s.get("https://www.nseindia.com", timeout=DEFAULT_TIMEOUT)
                time.sleep(0.4)
                s.get(
                    "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE",
                    timeout=DEFAULT_TIMEOUT,
                )
            except Exception as exc:
                log.warning("NSE warmup failed: %s", exc)
            _nse_session = s
            _nse_session_ts = time.time()
        return _nse_session


# --------------------------------------------------------------------------- #
#  Company list cache (NSE EQUITY_L.csv + BSE ListofScripCode)
# --------------------------------------------------------------------------- #

_companies: List[Dict[str, Any]] = []
_companies_ts: float = 0.0
_companies_lock = threading.Lock()
_companies_refresh_in_progress = False


def fetch_nse_equity_list() -> List[Dict[str, Any]]:
    """Download NSE EQUITY_L.csv and return company dicts."""
    s = nse_session()
    url = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    r = s.get(url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    rows: List[Dict[str, Any]] = []
    reader = csv.reader(io.StringIO(r.text))
    header = next(reader, None)
    if not header:
        return rows
    # Header columns vary in whitespace; build an index lookup
    idx = {h.strip().upper(): i for i, h in enumerate(header)}

    def at(row: List[str], key: str) -> str:
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in reader:
        if len(row) < 2:
            continue
        sym = at(row, "SYMBOL")
        name = at(row, "NAME OF COMPANY")
        isin = at(row, "ISIN NUMBER")
        listing = at(row, "DATE OF LISTING")
        face = at(row, "FACE VALUE")
        if not sym or not name:
            continue
        rows.append({
            "symbol": sym,
            "name": name,
            "exchange": "NSE",
            "isin": isin,
            "listing_date": listing,
            "face_value": face,
            "yahoo": f"{sym}.NS",
            "bse_code": None,
        })
    log.info("Fetched %d NSE equities", len(rows))
    return rows


def fetch_bse_scrip_list() -> List[Dict[str, Any]]:
    """Download BSE active equities list."""
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/ListofScripCode/w"
        "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
    )
    headers = _common_headers({
        "Origin": "https://www.bseindia.com",
        "Referer": "https://www.bseindia.com/",
    })
    r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
    r.raise_for_status()
    data = r.json()
    items = data.get("Table") or data.get("table") or []
    rows: List[Dict[str, Any]] = []
    for item in items:
        # BSE field names vary; try both casings
        code = str(item.get("SCRIP_CD") or item.get("scrip_cd") or item.get("Scrip_Cd") or "").strip()
        name = (item.get("scrip_name") or item.get("SCRIP_NAME") or item.get("Scrip_Name") or "").strip()
        sym = (item.get("scrip_id") or item.get("SCRIP_ID") or item.get("Scrip_Id") or "").strip()
        isin = (item.get("ISIN_NUMBER") or item.get("isin_number") or "").strip()
        if not code or not name:
            continue
        rows.append({
            "symbol": sym or code,
            "name": name,
            "exchange": "BSE",
            "isin": isin,
            "listing_date": "",
            "face_value": "",
            "yahoo": f"{code}.BO",
            "bse_code": code,
        })
    log.info("Fetched %d BSE scrips", len(rows))
    return rows


def _merge_companies(nse: List[Dict[str, Any]], bse: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prefer NSE entry but enrich with BSE code where ISIN matches."""
    bse_by_isin = {c["isin"]: c for c in bse if c.get("isin")}
    bse_by_name = {c["name"].upper(): c for c in bse}
    merged: List[Dict[str, Any]] = []
    seen_isins = set()
    for c in nse:
        match = None
        if c.get("isin") and c["isin"] in bse_by_isin:
            match = bse_by_isin[c["isin"]]
        elif c["name"].upper() in bse_by_name:
            match = bse_by_name[c["name"].upper()]
        if match:
            c["bse_code"] = match["bse_code"]
        merged.append(c)
        if c.get("isin"):
            seen_isins.add(c["isin"])
    # Append BSE-only listings
    for c in bse:
        if c.get("isin") and c["isin"] in seen_isins:
            continue
        merged.append(c)
    return merged


def refresh_companies(force: bool = False) -> None:
    """Refresh in-memory company cache. Safe to call concurrently."""
    global _companies, _companies_ts, _companies_refresh_in_progress
    with _companies_lock:
        if _companies_refresh_in_progress:
            return
        if not force and _companies and time.time() - _companies_ts < 60 * 60 * 12:
            return
        _companies_refresh_in_progress = True

    try:
        try:
            nse = fetch_nse_equity_list()
        except Exception as exc:
            log.error("NSE list fetch failed: %s", exc)
            nse = []
        try:
            bse = fetch_bse_scrip_list()
        except Exception as exc:
            log.error("BSE list fetch failed: %s", exc)
            bse = []
        merged = _merge_companies(nse, bse) if (nse or bse) else []
        with _companies_lock:
            if merged:
                _companies = merged
                _companies_ts = time.time()
                log.info("Companies cache: %d entries", len(_companies))
    finally:
        with _companies_lock:
            _companies_refresh_in_progress = False


# Kick off background refresh on import (Render boot)
threading.Thread(target=refresh_companies, daemon=True).start()


# --------------------------------------------------------------------------- #
#  Screener.in scraper for fundamentals
# --------------------------------------------------------------------------- #

def _to_number(s: str) -> Optional[float]:
    if s is None:
        return None
    s = s.strip().replace(",", "").replace("₹", "").replace("%", "")
    if s in ("", "-", "—"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _parse_screener_table(section) -> Optional[Dict[str, Any]]:
    if section is None:
        return None
    table = section.find("table")
    if table is None:
        return None
    thead = table.find("thead")
    tbody = table.find("tbody")
    if thead is None or tbody is None:
        return None
    headers = [th.get_text(strip=True) for th in thead.find_all("th")]
    if len(headers) < 2:
        return None
    periods = headers[1:]
    out_rows: List[Dict[str, Any]] = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        label = tds[0].get_text(" ", strip=True)
        label = re.sub(r"\s*\+\s*$", "", label).strip()
        values: List[Optional[float]] = []
        for td in tds[1:]:
            values.append(_to_number(td.get_text(strip=True)))
        out_rows.append({"label": label, "values": values})
    return {"periods": periods, "rows": out_rows}


def _scrape_screener_url(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, headers=_common_headers(), timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
        if r.status_code != 200:
            return None
        if "<table" not in r.text.lower():
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception as exc:
        log.warning("Screener fetch failed for %s: %s", url, exc)
        return None


def scrape_screener(symbol: str) -> Optional[Dict[str, Any]]:
    """Scrape Screener.in. Tries consolidated first, falls back to standalone."""
    sym = symbol.upper().strip()
    candidates = [
        f"https://www.screener.in/company/{sym}/consolidated/",
        f"https://www.screener.in/company/{sym}/",
    ]
    soup: Optional[BeautifulSoup] = None
    used_url: Optional[str] = None
    used_mode = "standalone"
    for i, url in enumerate(candidates):
        soup = _scrape_screener_url(url)
        if soup is not None:
            used_url = url
            used_mode = "consolidated" if i == 0 else "standalone"
            break
    if soup is None:
        return None

    # Top ratios (PE, market cap, etc.)
    top_ratios: Dict[str, str] = {}
    top_ul = soup.find("ul", id="top-ratios")
    if top_ul:
        for li in top_ul.find_all("li"):
            name_el = li.find("span", class_="name")
            val_el = li.find("span", class_="number")
            if not val_el:
                val_el = li.find("span", class_="value")
            if name_el and val_el:
                top_ratios[name_el.get_text(strip=True)] = val_el.get_text(" ", strip=True)

    # Section tables
    pl = _parse_screener_table(soup.find(id="profit-loss"))
    bs = _parse_screener_table(soup.find(id="balance-sheet"))
    cf = _parse_screener_table(soup.find(id="cash-flow"))
    qtr = _parse_screener_table(soup.find(id="quarters"))
    ratios = _parse_screener_table(soup.find(id="ratios"))
    shareholding = _parse_screener_table(soup.find(id="shareholding"))

    return {
        "url": used_url,
        "mode": used_mode,
        "top_ratios": top_ratios,
        "income_statement": pl,
        "balance_sheet": bs,
        "cash_flow": cf,
        "quarterly": qtr,
        "ratios_history": ratios,
        "shareholding": shareholding,
        "source": "Screener.in",
    }


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def root():
    with _companies_lock:
        n = len(_companies)
        ts = _companies_ts
    return jsonify({
        "service": "equity-research",
        "endpoints": [
            "/search?q=<query>",
            "/quote/<symbol>",
            "/news/<bsecode>",
            "/price/<symbol>?range=max&interval=1d&exchange=NS",
            "/fundamentals/<symbol>",
        ],
        "companies_loaded": n,
        "companies_loaded_at": datetime.utcfromtimestamp(ts).isoformat() + "Z" if ts else None,
        "proxy_configured": bool(PROXY_URL),
    })


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat() + "Z"})


def search_nse_autocomplete(q: str) -> List[Dict[str, Any]]:
    """Use NSE's live autocomplete. Same domain as /quote, so reachable from Render."""
    s = nse_session()
    url = f"https://www.nseindia.com/api/search/autocomplete?q={q}"
    try:
        r = s.get(url, timeout=DEFAULT_TIMEOUT, headers={"Referer": "https://www.nseindia.com/"})
        if r.status_code != 200:
            return []
        data = r.json()
        out: List[Dict[str, Any]] = []
        for item in (data.get("symbols") or []):
            sym = (item.get("symbol") or "").strip()
            info = (item.get("symbol_info") or "").strip()
            if not sym:
                continue
            out.append({
                "symbol": sym,
                "name": info or sym,
                "exchange": "NSE",
                "yahoo": f"{sym}.NS",
                "isin": item.get("symbol_isin", ""),
                "bse_code": None,
                "listing_date": "",
                "_source": "NSE autocomplete",
            })
        return out
    except Exception as exc:
        log.warning("NSE autocomplete failed: %s", exc)
        return []


def search_screener(q: str) -> List[Dict[str, Any]]:
    """Screener.in's public search. Returns hits across NSE+BSE."""
    url = f"https://www.screener.in/api/company/search/?q={q}"
    try:
        r = requests.get(url, headers=_common_headers(), timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
        if r.status_code != 200:
            return []
        data = r.json()
        out: List[Dict[str, Any]] = []
        for item in (data if isinstance(data, list) else []):
            sym = (item.get("url") or "").strip("/").split("/")[-1].upper()
            if not sym:
                sym = (item.get("name") or "").upper()
            out.append({
                "symbol": sym,
                "name": item.get("name") or sym,
                "exchange": "NSE" if not sym.isdigit() else "BSE",
                "yahoo": f"{sym}.NS" if not sym.isdigit() else f"{sym}.BO",
                "isin": "",
                "bse_code": sym if sym.isdigit() else None,
                "listing_date": "",
                "_source": "Screener.in",
            })
        return out
    except Exception as exc:
        log.warning("Screener search failed: %s", exc)
        return []


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": [], "total_universe": len(_companies)})

    # Source 1: cached NSE+BSE master (only populated if archives reachable)
    matches_cached: List[tuple] = []
    if _companies:
        qu = q.upper()
        for c in _companies:
            sym = c["symbol"].upper()
            name = c["name"].upper()
            score = 0
            if sym == qu: score = 100
            elif sym.startswith(qu): score = 92
            elif name.startswith(qu): score = 85
            elif qu in sym: score = 70
            elif qu in name: score = 55
            if score > 0:
                if c["exchange"] == "NSE": score += 1
                matches_cached.append((score, c))
        matches_cached.sort(key=lambda t: (-t[0], t[1]["name"]))
        if matches_cached:
            return jsonify({
                "query": q,
                "results": [c for _, c in matches_cached[:25]],
                "total_universe": len(_companies),
                "source": "cached NSE+BSE master",
            })

    # Source 2: NSE autocomplete (live)
    results = search_nse_autocomplete(q)

    # Source 3: Screener supplement (catches BSE-only and adds bse_code lookups)
    screener_hits = search_screener(q)
    seen = {r["symbol"] for r in results}
    for h in screener_hits:
        if h["symbol"] not in seen:
            results.append(h)
            seen.add(h["symbol"])

    return jsonify({
        "query": q,
        "results": results[:25],
        "total_universe": len(_companies),
        "source": "NSE autocomplete + Screener.in (live)",
    })


@app.route("/refresh-companies")
def refresh_companies_route():
    """Force a refresh. Useful after deploy."""
    refresh_companies(force=True)
    return jsonify({"ok": True, "loaded": len(_companies)})


@app.route("/quote/<symbol>")
def quote(symbol: str):
    sym = symbol.upper().strip()
    s = nse_session()
    url = f"https://www.nseindia.com/api/quote-equity?symbol={sym}"
    headers = {"Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"}
    try:
        r = s.get(url, timeout=DEFAULT_TIMEOUT, headers=headers)
        r.raise_for_status()
        data = r.json()
        data["_source"] = "NSE"
        return jsonify(data)
    except Exception as exc:
        log.error("/quote/%s failed: %s", sym, exc)
        return jsonify({"error": str(exc), "symbol": sym}), 502


def fetch_nse_announcements(symbol: str) -> Optional[List[Dict[str, Any]]]:
    """NSE corporate announcements for an equity symbol. Uses nseindia.com (reachable)."""
    s = nse_session()
    url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={symbol}"
    try:
        r = s.get(
            url,
            timeout=DEFAULT_TIMEOUT,
            headers={"Referer": f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"},
        )
        if r.status_code != 200:
            return None
        items = r.json() if isinstance(r.json(), list) else []
        out = []
        for it in items:
            attach = it.get("attchmntFile")
            link = attach if attach and attach.startswith("http") else (
                f"https://nsearchives.nseindia.com/{attach.lstrip('/')}" if attach else None
            )
            out.append({
                "headline": (it.get("attchmntText") or it.get("desc") or it.get("smIndustry") or "").strip(),
                "category": (it.get("desc") or it.get("smIndustry") or "").strip(),
                "date": it.get("an_dt") or it.get("sort_date") or "",
                "url": link,
            })
        return out
    except Exception as exc:
        log.warning("NSE announcements failed for %s: %s", symbol, exc)
        return None


def fetch_bse_announcements(bsecode: str) -> Optional[List[Dict[str, Any]]]:
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?strCat=-1&strPrevDate=&strScrip={bsecode}&strSearch=P&strToDate=&strType=C"
    )
    headers = _common_headers({
        "Origin": "https://www.bseindia.com",
        "Referer": "https://www.bseindia.com/",
    })
    try:
        r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
        if r.status_code != 200:
            return None
        items = r.json().get("Table") or []
        out = []
        for it in items:
            attach = it.get("ATTACHMENTNAME")
            link = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}" if attach else None
            out.append({
                "headline": (it.get("HEADLINE") or it.get("NEWSSUB") or "").strip(),
                "category": (it.get("CATEGORYNAME") or it.get("SUBCATNAME") or "").strip(),
                "date": it.get("NEWS_DT") or it.get("News_submission_dt") or "",
                "url": link,
            })
        return out
    except Exception as exc:
        log.warning("BSE announcements failed for %s: %s", bsecode, exc)
        return None


@app.route("/news/<identifier>")
def news(identifier: str):
    """Accepts NSE symbol (preferred) OR BSE numeric code. Tries NSE first, then BSE."""
    ident = identifier.strip().upper()
    sources_tried: List[str] = []

    # Symbol (alphabetical) -> NSE first
    if not ident.isdigit():
        sources_tried.append("NSE")
        out = fetch_nse_announcements(ident)
        if out is not None:
            return jsonify({
                "id": ident,
                "count": len(out),
                "items": out,
                "_source": "NSE Corporate Announcements",
            })

    # Numeric -> BSE
    if ident.isdigit():
        sources_tried.append("BSE")
        out = fetch_bse_announcements(ident)
        if out is not None:
            return jsonify({
                "id": ident,
                "count": len(out),
                "items": out,
                "_source": "BSE Corporate Announcements",
            })

    return jsonify({"error": "no announcements", "id": ident, "tried": sources_tried}), 502


RANGE_TO_DAYS = {
    "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365,
    "2y": 730, "3y": 1095, "5y": 1825, "10y": 3650, "max": 7300,
}


def _parse_nse_date(s: str) -> Optional[str]:
    """NSE returns dates like '24-Apr-2026' or '2024-04-24T00:00:00.000Z'."""
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    return s


def fetch_nse_history(symbol: str, days: int) -> List[Dict[str, Any]]:
    """NSE historical OHLC. Paginates yearly because NSE caps each call at ~365d."""
    s = nse_session()
    referer = f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}"
    bars: List[Dict[str, Any]] = []
    seen_dates = set()

    end = datetime.utcnow()
    target_start = end - timedelta(days=days)
    cursor_end = end
    max_chunks = (days // 365) + 2  # safety cap

    for _ in range(max_chunks):
        chunk_start = cursor_end - timedelta(days=365)
        if chunk_start < target_start:
            chunk_start = target_start
        url = "https://www.nseindia.com/api/historical/cm/equity"
        params = {
            "symbol": symbol,
            "series": '["EQ"]',
            "from": chunk_start.strftime("%d-%m-%Y"),
            "to": cursor_end.strftime("%d-%m-%Y"),
        }
        try:
            r = s.get(url, params=params, timeout=DEFAULT_TIMEOUT, headers={"Referer": referer})
            if r.status_code != 200:
                log.warning("NSE history %s status=%s", symbol, r.status_code)
                break
            data = r.json().get("data") or []
            if not data:
                break
            chunk_bars = []
            for it in data:
                d = _parse_nse_date(it.get("CH_TIMESTAMP") or it.get("mTIMESTAMP") or "")
                if not d or d in seen_dates:
                    continue
                seen_dates.add(d)
                def n(*keys):
                    for k in keys:
                        v = it.get(k)
                        if v is not None and v != "":
                            try:
                                return float(v)
                            except Exception:
                                pass
                    return None
                chunk_bars.append({
                    "date": d,
                    "open": n("CH_OPENING_PRICE", "open"),
                    "high": n("CH_TRADE_HIGH_PRICE", "high"),
                    "low": n("CH_TRADE_LOW_PRICE", "low"),
                    "close": n("CH_CLOSING_PRICE", "close"),
                    "volume": int(n("CH_TOT_TRADED_QTY", "volume") or 0),
                })
            bars.extend(chunk_bars)
            if chunk_start <= target_start:
                break
            cursor_end = chunk_start - timedelta(days=1)
        except Exception as exc:
            log.warning("NSE history chunk failed for %s: %s", symbol, exc)
            break

    bars.sort(key=lambda b: b["date"])
    return bars


def fetch_yahoo_chart(ticker: str, range_: str, interval: str) -> Optional[Dict[str, Any]]:
    """Direct call to Yahoo's v8 chart endpoint. Tries query1 then query2."""
    headers = _common_headers({
        "Accept": "application/json",
        "Origin": "https://finance.yahoo.com",
        "Referer": f"https://finance.yahoo.com/quote/{ticker}",
    })
    params = {"range": range_, "interval": interval, "includeAdjustedClose": "true"}
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{ticker}"
        try:
            r = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
            if r.status_code != 200:
                continue
            data = r.json()
            result = (data.get("chart") or {}).get("result")
            if not result:
                continue
            res = result[0]
            timestamps = res.get("timestamp") or []
            ind = res.get("indicators") or {}
            quote = (ind.get("quote") or [{}])[0]
            def at(arr, idx):
                if idx >= len(arr) or arr[idx] is None:
                    return None
                try:
                    v = float(arr[idx])
                    return v if v == v else None
                except Exception:
                    return None
            bars = []
            for i, ts in enumerate(timestamps):
                bars.append({
                    "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                    "open": at(quote.get("open") or [], i),
                    "high": at(quote.get("high") or [], i),
                    "low": at(quote.get("low") or [], i),
                    "close": at(quote.get("close") or [], i),
                    "volume": int((quote.get("volume") or [0])[i]) if i < len(quote.get("volume") or []) and (quote.get("volume") or [])[i] is not None else 0,
                })
            meta = res.get("meta") or {}
            return {
                "ticker": ticker,
                "currency": meta.get("currency"),
                "exchange": meta.get("exchangeName"),
                "bars": bars,
            }
        except Exception as exc:
            log.warning("Yahoo chart %s via %s failed: %s", ticker, host, exc)
            continue
    return None


@app.route("/price/<symbol>")
def price(symbol: str):
    import traceback
    try:
        sym = symbol.upper().strip()
        range_param = request.args.get("range", "max")
        interval = request.args.get("interval", "1d")
        exchange = request.args.get("exchange", "NS").upper()

        days = RANGE_TO_DAYS.get(range_param, 7300)

        # Source 1: NSE historical (works because /quote works)
        nse_err = None
        if "." not in sym and exchange != "BO":
            try:
                bars = fetch_nse_history(sym, days)
            except Exception as exc:
                log.exception("NSE history raised for %s", sym)
                bars = []
                nse_err = str(exc)
            if bars:
                return jsonify({
                    "ticker": sym,
                    "range": range_param,
                    "interval": interval,
                    "count": len(bars),
                    "first": bars[0]["date"],
                    "last": bars[-1]["date"],
                    "currency": "INR",
                    "exchange": "NSE",
                    "bars": bars,
                    "_source": "NSE Historical",
                })

        # Source 2: Yahoo (fallback, including .BO requests)
        candidates: List[str] = []
        if "." in sym:
            candidates.append(sym)
        else:
            candidates.extend([f"{sym}.BO", f"{sym}.NS"] if exchange == "BO" else [f"{sym}.NS", f"{sym}.BO"])
        yf_err = None
        for ticker in candidates:
            try:
                result = fetch_yahoo_chart(ticker, range_param, interval)
            except Exception as exc:
                log.exception("Yahoo chart raised for %s", ticker)
                result = None
                yf_err = str(exc)
            if result and result.get("bars"):
                bars = result["bars"]
                return jsonify({
                    "ticker": result["ticker"],
                    "range": range_param,
                    "interval": interval,
                    "count": len(bars),
                    "first": bars[0]["date"] if bars else None,
                    "last": bars[-1]["date"] if bars else None,
                    "currency": result.get("currency"),
                    "exchange": result.get("exchange"),
                    "bars": bars,
                    "_source": "Yahoo Finance v8/chart",
                })

        return jsonify({
            "error": "no data from NSE or Yahoo",
            "tried": candidates or [sym],
            "symbol": sym,
            "nse_error": nse_err,
            "yahoo_error": yf_err,
        }), 502
    except Exception as exc:
        log.exception("/price/%s crashed", symbol)
        return jsonify({
            "error": "unexpected error",
            "detail": str(exc),
            "trace": traceback.format_exc().splitlines()[-5:],
        }), 500


@app.route("/fundamentals/<symbol>")
def fundamentals(symbol: str):
    sym = symbol.upper().strip()
    data = scrape_screener(sym)
    if data is None:
        return jsonify({
            "error": "screener fetch failed",
            "symbol": sym,
            "fallback_links": {
                "screener": f"https://www.screener.in/company/{sym}/",
                "nse": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}",
            },
        }), 502

    # Compute simple CAGR helpers from income statement
    cagrs: Dict[str, Dict[str, Optional[float]]] = {}
    pl = data.get("income_statement") or {}
    rows = pl.get("rows") or []
    periods = pl.get("periods") or []

    def find_row(label_substr: str) -> Optional[List[Optional[float]]]:
        ls = label_substr.lower()
        for r in rows:
            if ls in r["label"].lower():
                return r["values"]
        return None

    def cagr(values: List[Optional[float]], years: int) -> Optional[float]:
        if not values or len(values) < years + 1:
            return None
        # Screener tables: leftmost = oldest, rightmost = TTM/latest. Skip TTM column if present
        usable = values[:-1] if periods and "TTM" in (periods[-1] or "").upper() else values
        if len(usable) < years + 1:
            return None
        v0 = usable[-(years + 1)]
        v1 = usable[-1]
        if v0 is None or v1 is None or v0 <= 0:
            return None
        try:
            return ((v1 / v0) ** (1 / years) - 1) * 100
        except Exception:
            return None

    revenue = find_row("sales") or find_row("revenue")
    net_profit = find_row("net profit")
    if revenue:
        cagrs["revenue"] = {
            "3y": cagr(revenue, 3),
            "5y": cagr(revenue, 5),
            "10y": cagr(revenue, 10),
        }
    if net_profit:
        cagrs["net_profit"] = {
            "3y": cagr(net_profit, 3),
            "5y": cagr(net_profit, 5),
            "10y": cagr(net_profit, 10),
        }

    data["cagrs"] = cagrs
    data["symbol"] = sym
    return jsonify(data)


# --------------------------------------------------------------------------- #
#  Entrypoint
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
