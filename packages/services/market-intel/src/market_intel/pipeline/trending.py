"""Scan StockTwits for trending equities to feed downstream enrichment.

Fetches trending equities and enriches each with basic sentiment/watchlist
data from the stream endpoint. Outputs trending.jsonl — a separate discovery
source from the insider-trading pipeline.

Auth: set STOCKTWITS_USER and STOCKTWITS_PASS env vars before running.

Usage (standalone):
    python -m market_intel.pipeline.trending                     # writes trending.jsonl
    python -m market_intel.pipeline.trending out.jsonl           # custom output path
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx

_API_BASE = "https://api.stocktwits.com/api/2"
_USER = os.environ.get("STOCKTWITS_USER", "")
_PASS = os.environ.get("STOCKTWITS_PASS", "")


def _auth() -> tuple[str, str] | None:
    if _USER and _PASS:
        return (_USER, _PASS)
    return None


_ETF_KEYWORDS = {"etf", "2x", "3x", "inverse", "daily target", "select sector",
                  "spdr", "ishares", "proshares", "direxion", "roundhill",
                  "tradr", "defiance", "t-rex", "global x"}


def _is_real_equity(sym: dict) -> bool:
    """Filter out crypto (.X suffix) and leveraged/inverse ETFs."""
    ticker = sym.get("symbol", "")
    if ".X" in ticker:
        return False
    title = (sym.get("title") or "").lower()
    return not any(kw in title for kw in _ETF_KEYWORDS)


def fetch_trending_equities() -> list[dict]:
    """Fetch trending equity symbols from StockTwits, filtering crypto and ETFs.

    Returns a list of symbol dicts with ticker, title, and watchlist_count.
    """
    auth = _auth()
    try:
        resp = httpx.get(
            f"{_API_BASE}/trending/symbols/equities.json",
            auth=auth, timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        symbols = data.get("symbols", [])
        return [s for s in symbols if _is_real_equity(s)]
    except Exception as e:
        print(f"  trending fetch error: {e}", file=sys.stderr)
        return []


def enrich_trending_symbol(sym: dict) -> dict:
    """Build a normalised record from a trending symbol.

    Pulls additional stream data (sentiment, message volume) for each
    trending ticker to give downstream agents more context.
    """
    ticker = sym.get("symbol", "")
    auth = _auth()

    record = {
        "ticker": ticker,
        "company": sym.get("title", ""),
        "source": "stocktwits_trending",
        "watchlist_count": sym.get("watchlist_count", 0),
    }

    # Pull stream data for sentiment + message volume
    try:
        resp = httpx.get(
            f"{_API_BASE}/streams/symbol/{ticker}.json",
            auth=auth, timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

        sym_data = data.get("symbol", {})
        record["watchlist_count"] = sym_data.get("watchlist_count", record["watchlist_count"])

        msgs = data.get("messages", [])
        bull = bear = 0
        for m in msgs:
            basic = ((m.get("entities") or {}).get("sentiment") or {}).get("basic", "")
            if basic == "Bullish":
                bull += 1
            elif basic == "Bearish":
                bear += 1

        tagged = bull + bear
        record["sentiment"] = {
            "available": True,
            "recent_messages": len(msgs),
            "recent_bullish": bull,
            "recent_bearish": bear,
            "bullish_pct": round(bull / tagged * 100, 2) if tagged else None,
            "bearish_pct": round(bear / tagged * 100, 2) if tagged else None,
        }
    except Exception as e:
        print(f"  stream error {ticker}: {e}", file=sys.stderr)
        record["sentiment"] = {"available": False}

    return record


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "trending.jsonl"

    if not _USER or not _PASS:
        print("warning: STOCKTWITS_USER/STOCKTWITS_PASS not set",
              file=sys.stderr)

    print("fetching trending equities...", file=sys.stderr)
    symbols = fetch_trending_equities()

    if not symbols:
        print("no trending symbols found", file=sys.stderr)
        return 1

    print(f"found {len(symbols)} trending equities, enriching...", file=sys.stderr)
    scanned_at = datetime.now(timezone.utc).isoformat()

    records: list[dict] = []
    for sym in symbols:
        ticker = sym.get("symbol", "")
        if not ticker:
            continue
        print(f"  enriching: {ticker}", file=sys.stderr)
        record = enrich_trending_symbol(sym)
        record["scanned_at"] = scanned_at
        records.append(record)
        time.sleep(0.3)  # rate-limit courtesy

    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"wrote {len(records)} trending tickers -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
