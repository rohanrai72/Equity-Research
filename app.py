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
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

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


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": [], "total_universe": len(_companies)})
    if not _companies:
        refresh_companies(force=True)
    qu = q.upper()
    matches: List[tuple] = []
    for c in _companies:
        sym = c["symbol"].upper()
        name = c["name"].upper()
        score = 0
        if sym == qu:
            score = 100
        elif sym.startswith(qu):
            score = 92
        elif name.startswith(qu):
            score = 85
        elif qu in sym:
            score = 70
        elif qu in name:
            score = 55
        if score > 0:
            # Slightly prefer NSE over BSE-only to surface liquid names first
            if c["exchange"] == "NSE":
                score += 1
            matches.append((score, c))
    matches.sort(key=lambda t: (-t[0], t[1]["name"]))
    results = [c for _, c in matches[:25]]
    return jsonify({
        "query": q,
        "results": results,
        "total_universe": len(_companies),
        "source": "NSE EQUITY_L.csv + BSE ListofScripCode",
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


@app.route("/news/<bsecode>")
def news(bsecode: str):
    code = re.sub(r"\D", "", bsecode)
    if not code:
        return jsonify({"error": "invalid bse code"}), 400
    url = (
        "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
        f"?strCat=-1&strPrevDate=&strScrip={code}&strSearch=P&strToDate=&strType=C"
    )
    headers = _common_headers({
        "Origin": "https://www.bseindia.com",
        "Referer": "https://www.bseindia.com/",
    })
    try:
        r = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT, proxies=PROXIES)
        r.raise_for_status()
        data = r.json()
        items = data.get("Table") or []
        normalized = []
        for it in items:
            headline = it.get("HEADLINE") or it.get("NEWSSUB") or ""
            cat = it.get("CATEGORYNAME") or it.get("SUBCATNAME") or ""
            dt = it.get("NEWS_DT") or it.get("News_submission_dt") or ""
            attach = it.get("ATTACHMENTNAME")
            link = (
                f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}"
                if attach else None
            )
            normalized.append({
                "headline": headline.strip(),
                "category": cat.strip(),
                "date": dt,
                "url": link,
            })
        return jsonify({
            "bse_code": code,
            "count": len(normalized),
            "items": normalized,
            "_source": "BSE Corporate Announcements",
        })
    except Exception as exc:
        log.error("/news/%s failed: %s", code, exc)
        return jsonify({"error": str(exc), "bse_code": code}), 502


@app.route("/price/<symbol>")
def price(symbol: str):
    if yf is None:
        return jsonify({"error": "yfinance not installed"}), 500
    sym = symbol.upper().strip()
    range_param = request.args.get("range", "max")
    interval = request.args.get("interval", "1d")
    exchange = request.args.get("exchange", "NS").upper()

    # Build candidate tickers — the same symbol may exist on NSE or BSE
    candidates: List[str] = []
    if "." in sym:
        candidates.append(sym)
    else:
        if exchange == "BO":
            candidates.append(f"{sym}.BO")
            candidates.append(f"{sym}.NS")
        else:
            candidates.append(f"{sym}.NS")
            candidates.append(f"{sym}.BO")

    last_err: Optional[str] = None
    for ticker in candidates:
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=range_param, interval=interval, auto_adjust=False)
            if df.empty:
                continue
            bars = []
            for ts, row in df.iterrows():
                def fnum(v):
                    try:
                        v = float(v)
                        return v if v == v else None  # NaN check
                    except Exception:
                        return None
                bars.append({
                    "date": ts.strftime("%Y-%m-%d"),
                    "open": fnum(row.get("Open")),
                    "high": fnum(row.get("High")),
                    "low": fnum(row.get("Low")),
                    "close": fnum(row.get("Close")),
                    "volume": int(row["Volume"]) if row.get("Volume") == row.get("Volume") else 0,
                })
            return jsonify({
                "ticker": ticker,
                "range": range_param,
                "interval": interval,
                "count": len(bars),
                "first": bars[0]["date"] if bars else None,
                "last": bars[-1]["date"] if bars else None,
                "bars": bars,
                "_source": "Yahoo Finance",
            })
        except Exception as exc:
            last_err = str(exc)
            log.warning("yfinance %s failed: %s", ticker, exc)
            continue

    return jsonify({
        "error": last_err or "no data",
        "tried": candidates,
        "symbol": sym,
    }), 502


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
