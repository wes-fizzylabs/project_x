"""Scan SEC 13F filings for institutional holders of universe tickers.

Queries the EDGAR EFTS (full-text search) API to find how many institutional
managers hold each ticker in the universe, and flags when notable/well-known
funds appear in the results.

Runs after merge_sources.py and before enrich_universe.py so that
institutional data is available for downstream scoring.

Usage (standalone):
    python -m market_intel.pipeline.sec13f                            # defaults
    python -m market_intel.pipeline.sec13f universe.jsonl out.jsonl
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import date, timedelta

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

UA = "OpenInsiderScraper/0.1 (research use)"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

# EDGAR rate limit: 10 req/sec — stay safely under
REQUEST_DELAY = 0.12

# How far back to look for 13F filings.
# 13F deadline is 45 days after quarter end. With quarters ending Mar/Jun/Sep/Dec,
# filings trickle in over ~2 months. 90 days captures the full filing season.
LOOKBACK_DAYS = 90

# Max results to fetch per ticker (for notable fund detection)
PAGE_SIZE = 100

# Notable institutional managers — CIK -> display name
# These get flagged when they appear as holders of a universe ticker.
# Add/remove entries as needed.
NOTABLE_FUNDS: dict[str, str] = {
    "0001067983": "Berkshire Hathaway",
    "0001350694": "Bridgewater Associates",
    "0001336528": "Pershing Square Capital",
    "0001029160": "Soros Fund Management",
    "0001536411": "Duquesne Family Office",
    "0001079114": "Greenlight Capital",
    "0001167483": "Tiger Global Management",
    "0001423053": "Citadel Advisors",
    "0001037389": "Renaissance Technologies",
    "0001649339": "Appaloosa Management",
    "0001510761": "Two Sigma Investments",
    "0001061768": "Third Point",
    "0001159159": "Viking Global Investors",
    "0001061165": "Baupost Group",
    "0001345471": "DE Shaw & Co",
    "0001103804": "Elliott Investment Management",
    "0001040273": "Millennium Management",
    "0001656456": "Point72 Asset Management",
    "0001697748": "Coatue Management",
    "0001582202": "Dragoneer Investment Group",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(
    r"\b(Inc|Incorporated|Corp|Corporation|Ltd|Limited|Co|Company|"
    r"Enterprises|Holdings|Group|LP|LLC|PLC|SA|NV|AG|SE)\b\.?",
    re.IGNORECASE,
)


def _clean_company_name(name: str, strip_suffixes: bool = False) -> str:
    """Normalize company name for EFTS phrase matching.

    13F nameOfIssuer values look like "APPLE INC", "Palantir Technologies Inc",
    "Danaher Corp".  Our universe has "Apple Inc", "Kailera Therapeutics, Inc.".

    When strip_suffixes=True, also removes corporate suffixes (Inc, Corp, etc.)
    for a broader fallback search.
    """
    # Strip SEC jurisdiction codes: /MO/, /DE/, /NY/ etc.
    name = re.sub(r"\s*/[A-Z]{2}/\s*", " ", name)
    # Strip share class descriptors: "- Ordinary Shares - Class A" etc.
    name = re.sub(
        r"\s*-?\s*(Ordinary Shares|Class [A-Z]|Common Stock|ADR|ADS).*$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    if strip_suffixes:
        name = _SUFFIX_RE.sub("", name)
    # Replace special chars that break EFTS phrase search
    name = name.replace("&", " ")   # AT&T -> AT T
    name = name.replace(".", " ")   # Amazon.com -> Amazon com
    name = name.replace("-", " ")   # Cleveland-Cliffs -> Cleveland Cliffs
    # Remove commas
    name = re.sub(r",\s*", " ", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _query_efts(
    search_term: str,
    client: httpx.Client,
    start_dt: str,
    end_dt: str,
) -> dict:
    """Run a single EFTS query and parse the results."""
    params = {
        "q": f'"{search_term}"',
        "forms": "13F-HR",
        "dateRange": "custom",
        "startdt": start_dt,
        "enddt": end_dt,
        "from": "0",
        "size": str(PAGE_SIZE),
    }

    try:
        resp = client.get(EFTS_URL, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"    EFTS error for {search_term!r}: {exc}", file=sys.stderr)
        return {"available": False, "total_holders": 0}

    hits = data.get("hits", {})
    total = hits.get("total", {}).get("value", 0)
    entries = hits.get("hits", [])

    # Check for notable funds in results
    notable_matches: list[dict] = []
    sample_holders: list[str] = []

    for entry in entries:
        src = entry.get("_source", {})
        ciks = src.get("ciks", [])
        names = src.get("display_names", [])
        filed = src.get("file_date", "")
        cik = ciks[0] if ciks else ""
        fund_display = names[0] if names else "Unknown"

        # Deduplicate sample holders by name
        short_name = fund_display.split("(CIK")[0].strip()
        if short_name not in sample_holders:
            sample_holders.append(short_name)

        if cik in NOTABLE_FUNDS:
            notable_matches.append({
                "fund": NOTABLE_FUNDS[cik],
                "cik": cik,
                "filed": filed,
            })

    return {
        "available": True,
        "total_holders": total,
        "notable_funds": notable_matches,
        "sample_holders": sample_holders[:10],
        "search_term": search_term,
        "period": f"{start_dt}/{end_dt}",
    }


def _search_efts(
    company_name: str,
    client: httpx.Client,
) -> dict:
    """Query EDGAR EFTS for 13F filings mentioning a company name.

    Tries the full cleaned name first. On 0 hits, retries with corporate
    suffixes stripped for a broader match (handles naming mismatches like
    'Babcock & Wilcox Enterprises Inc' vs '13F nameOfIssuer' variants).
    """
    clean = _clean_company_name(company_name)
    if not clean:
        return {"available": False, "total_holders": 0}

    start_dt = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_dt = date.today().isoformat()

    result = _query_efts(clean, client, start_dt, end_dt)

    # Fallback: strip corporate suffixes and retry if no hits
    if result.get("total_holders", 0) == 0:
        short = _clean_company_name(company_name, strip_suffixes=True)
        # Only retry if the shortened name is meaningfully different
        # and has at least 2 words (avoid single-word false positives)
        if short != clean and len(short.split()) >= 2:
            print(f"    0 hits for {clean!r}, retrying with {short!r}", file=sys.stderr)
            time.sleep(REQUEST_DELAY)
            result = _query_efts(short, client, start_dt, end_dt)

    return result


SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _fetch_sec_ticker_map(tickers: set[str]) -> dict[str, str]:
    """Look up company names for tickers via SEC company_tickers.json.

    Used as a fallback when universe records lack a company name
    (e.g., UOA-only records from scan_uoa.py).
    """
    try:
        resp = httpx.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": UA},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"    SEC ticker lookup failed: {exc}", file=sys.stderr)
        return {}

    # Build ticker -> title map from SEC data
    sec_map: dict[str, str] = {}
    for entry in data.values():
        sym = entry.get("ticker", "")
        title = entry.get("title", "")
        if sym in tickers and title:
            sec_map[sym] = title

    found = len(sec_map)
    missed = tickers - set(sec_map)
    if found:
        print(
            f"  SEC lookup: resolved {found} company names"
            + (f" ({len(missed)} still missing)" if missed else ""),
            file=sys.stderr,
        )
    return sec_map


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    in_path = sys.argv[1] if len(sys.argv) > 1 else "universe.jsonl"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "universe.jsonl"
    tmp_path = out_path + ".tmp"

    records: list[dict] = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Build unique ticker -> company name map
    ticker_company: dict[str, str] = {}
    for r in records:
        ticker = r["ticker"]
        company = r.get("company") or ""
        if ticker not in ticker_company and company:
            ticker_company[ticker] = company

    # Resolve missing company names via SEC company_tickers.json
    all_tickers = {r["ticker"] for r in records}
    missing = all_tickers - set(ticker_company)
    if missing:
        sec_lookup = _fetch_sec_ticker_map(missing)
        for ticker, company in sec_lookup.items():
            if company:
                ticker_company[ticker] = company

    if not ticker_company:
        print("no tickers to scan — skipping 13F lookup", file=sys.stderr)
        return 0

    # Query EFTS for each unique ticker
    results_cache: dict[str, dict] = {}

    with httpx.Client(headers={"User-Agent": UA}) as client:
        for i, (ticker, company) in enumerate(ticker_company.items()):
            print(
                f"  13F scan [{i + 1}/{len(ticker_company)}]: "
                f"{ticker} ({company})",
                file=sys.stderr,
            )
            results_cache[ticker] = _search_efts(company, client)
            time.sleep(REQUEST_DELAY)

    # Attach results to every record
    enriched = 0
    notable_total = 0
    for r in records:
        ticker = r["ticker"]
        if ticker in results_cache:
            r["institutional"] = results_cache[ticker]
            enriched += 1
            notable_total += len(
                results_cache[ticker].get("notable_funds", [])
            )

    # Atomic write
    with open(tmp_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)

    print(
        f"13F: {len(ticker_company)} tickers scanned, "
        f"{enriched} records enriched, "
        f"{notable_total} notable fund matches -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
