"""Merge insider trades, trending tickers, and UOA into a unified universe.

Reads three sources and merges on ticker into universe.jsonl:
  1. trades_final.jsonl  — insider pipeline (Form 4 purchases)
  2. trending.jsonl      — StockTwits trending equities
  3. uoa.jsonl           — unusual options activity scanner

Confirmation boosts stack when sources overlap:
  - Insider + trending:  1.15x composite boost
  - Insider + UOA:       1.25x composite boost
  - Insider + both:      1.15 × 1.25 = 1.4375x composite boost
  - Trending + UOA:      discovery_score boosted by UOA strength
  - UOA-only:            standalone record scored by uoa_score

Usage (standalone):
    python -m market_intel.pipeline.merge                                          # defaults
    python -m market_intel.pipeline.merge trades_final.jsonl trending.jsonl uoa.jsonl out.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import httpx


def _load_jsonl(path: str) -> list[dict]:
    records: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except FileNotFoundError:
        print(f"  warning: {path} not found, skipping", file=sys.stderr)
    return records


# ---------------------------------------------------------------------------
# Company name resolution
# ---------------------------------------------------------------------------

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_UA = "OpenInsiderScraper/0.1 (research use)"


def _fetch_company_names(tickers: set[str]) -> dict[str, str]:
    """Look up company names for tickers via SEC company_tickers.json."""
    if not tickers:
        return {}
    try:
        resp = httpx.get(
            _SEC_TICKERS_URL, headers={"User-Agent": _UA}, timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  SEC company name lookup failed: {exc}", file=sys.stderr)
        return {}

    result: dict[str, str] = {}
    for entry in data.values():
        sym = entry.get("ticker", "")
        title = entry.get("title", "")
        if sym in tickers and title:
            result[sym] = title

    if result:
        missed = tickers - set(result)
        print(
            f"  resolved {len(result)} company names via SEC"
            + (f" ({len(missed)} still missing)" if missed else ""),
            file=sys.stderr,
        )
    return result


# ---------------------------------------------------------------------------
# Discovery scoring for trending-only tickers
# ---------------------------------------------------------------------------

def _discovery_score(trend: dict) -> float:
    """Score a trending-only ticker by how "about to be discovered" it looks.

    Returns 0–100 scale (comparable to urgency_score conceptually).

    Components:
      - Watchlist factor (0–40): lower watchlist = more under the radar
      - Attention velocity (0–35): messages relative to watchlist size
      - Bullish momentum (0–25): strong bullish skew in recent messages
    """
    sent = trend.get("sentiment") or {}
    watchlist = trend.get("watchlist_count", 0) or 0
    msgs = sent.get("recent_messages", 0) or 0
    bull = sent.get("recent_bullish", 0) or 0
    bear = sent.get("recent_bearish", 0) or 0

    # --- watchlist factor: under the radar = highest score ---
    if watchlist < 2_000:
        wl_score = 40      # very obscure — ideal discovery
    elif watchlist < 5_000:
        wl_score = 35       # low profile
    elif watchlist < 10_000:
        wl_score = 25       # moderate following
    elif watchlist < 25_000:
        wl_score = 15       # well-known
    elif watchlist < 50_000:
        wl_score = 8        # popular
    else:
        wl_score = 3        # heavily watched — everyone already sees it

    # --- attention velocity: message spike relative to follower base ---
    # High messages on a low-watchlist ticker = sudden interest
    if watchlist > 0 and msgs > 0:
        velocity = msgs / (watchlist / 1000)  # msgs per 1k followers
        if velocity >= 10:
            vel_score = 35   # extreme spike
        elif velocity >= 5:
            vel_score = 28
        elif velocity >= 2:
            vel_score = 20
        elif velocity >= 1:
            vel_score = 12
        else:
            vel_score = 5    # normal chatter rate
    else:
        vel_score = 0

    # --- bullish momentum: strong directional sentiment ---
    if msgs > 0:
        bull_pct = bull / msgs * 100
        if bull_pct >= 80:
            bull_score = 25   # strong consensus forming
        elif bull_pct >= 60:
            bull_score = 18
        elif bull_pct >= 40:
            bull_score = 10   # mixed
        else:
            bull_score = 5    # bearish-leaning — contrarian interest
    else:
        bull_score = 0

    return round(wl_score + vel_score + bull_score, 1)


# ---------------------------------------------------------------------------
# Trending confirmation multiplier for overlap tickers
# ---------------------------------------------------------------------------

_TRENDING_CONFIRMATION_MULT = 1.15  # insider bought + stock is trending
_UOA_CONFIRMATION_MULT = 1.25       # insider bought + unusual options activity

_WATCHLIST_PATH = "watchlist.txt"
_AUTOMATION_HEADER = "# --- agent automation: insider discoveries ---"


def _sync_watchlist(insider_tickers: set[str]) -> int:
    """Append new insider tickers to watchlist.txt under the automation section.

    Reads existing watchlist, finds (or creates) the automation section,
    and adds any insider tickers not already present anywhere in the file.
    Returns the count of newly added tickers.
    """
    # Read existing lines
    existing_lines: list[str] = []
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8") as f:
            existing_lines = f.readlines()
    except FileNotFoundError:
        pass

    # Collect all tickers already in the file (any section)
    existing_tickers: set[str] = set()
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            existing_tickers.add(stripped.upper())

    new_tickers = sorted(insider_tickers - existing_tickers)
    if not new_tickers:
        return 0

    # Find automation section or append it
    header_idx = None
    for i, line in enumerate(existing_lines):
        if line.strip() == _AUTOMATION_HEADER:
            header_idx = i
            break

    if header_idx is None:
        # Add a blank line separator + header at the end
        if existing_lines and not existing_lines[-1].strip() == "":
            existing_lines.append("\n")
        existing_lines.append(f"{_AUTOMATION_HEADER}\n")
        header_idx = len(existing_lines) - 1

    # Find insertion point: right after last ticker in the automation section
    insert_at = header_idx + 1
    for i in range(header_idx + 1, len(existing_lines)):
        stripped = existing_lines[i].strip()
        if stripped.startswith("# ---") and stripped != _AUTOMATION_HEADER:
            break  # hit the next section
        insert_at = i + 1

    for ticker in new_tickers:
        existing_lines.insert(insert_at, f"{ticker}\n")
        insert_at += 1

    with open(_WATCHLIST_PATH, "w", encoding="utf-8") as f:
        f.writelines(existing_lines)

    return len(new_tickers)


# ---------------------------------------------------------------------------
# Helpers for attaching source overlays
# ---------------------------------------------------------------------------

def _attach_trending(record: dict, trend: dict) -> None:
    """Attach trending data and apply confirmation boost to an insider record."""
    record["sources"].append("stocktwits_trending")
    record["trending"] = {
        "watchlist_count": trend.get("watchlist_count"),
        "sentiment": trend.get("sentiment"),
        "scanned_at": trend.get("scanned_at"),
    }
    old_composite = record.get("composite_score") or 0
    record["composite_score"] = round(old_composite * _TRENDING_CONFIRMATION_MULT, 1)

    breakdown = record.get("score_breakdown", {})
    mults = breakdown.get("multipliers", {})
    mults["trending_confirmation"] = _TRENDING_CONFIRMATION_MULT
    breakdown["multipliers"] = mults
    record["score_breakdown"] = breakdown


def _attach_uoa(record: dict, uoa: dict) -> None:
    """Attach UOA data and apply confirmation boost to an insider record."""
    record["sources"].append("uoa_scan")
    record["uoa"] = {
        "uoa_score": uoa.get("uoa_score"),
        "unusual_contracts": uoa.get("unusual_contracts"),
        "total_dollar_volume": uoa.get("total_dollar_volume"),
        "max_vol_oi_ratio": uoa.get("max_vol_oi_ratio"),
        "dominant_expiry": uoa.get("dominant_expiry"),
        "call_put_ratio": uoa.get("call_put_ratio"),
        "otm_call_pct": uoa.get("otm_call_pct"),
        "top_contracts": uoa.get("top_contracts", [])[:3],
        "scanned_at": uoa.get("scanned_at"),
    }
    old_composite = record.get("composite_score") or 0
    record["composite_score"] = round(old_composite * _UOA_CONFIRMATION_MULT, 1)

    breakdown = record.get("score_breakdown", {})
    mults = breakdown.get("multipliers", {})
    mults["uoa_confirmation"] = _UOA_CONFIRMATION_MULT
    breakdown["multipliers"] = mults
    record["score_breakdown"] = breakdown


def _build_non_insider_record(
    ticker: str,
    sources: list[str],
    trend: dict | None,
    uoa: dict | None,
    merged_at: str,
    company: str = "",
) -> dict:
    """Build a record for a ticker with no insider trade data."""
    disc_score = None
    disc_signals = None
    if trend:
        disc_score = _discovery_score(trend)
        disc_signals = {
            "watchlist_count": trend.get("watchlist_count", 0),
            "recent_messages": (trend.get("sentiment") or {}).get("recent_messages", 0),
            "recent_bullish": (trend.get("sentiment") or {}).get("recent_bullish", 0),
            "recent_bearish": (trend.get("sentiment") or {}).get("recent_bearish", 0),
        }

    # If both trending and UOA, boost discovery by UOA strength
    if disc_score is not None and uoa:
        uoa_strength = (uoa.get("uoa_score") or 0) / 100  # 0.0–1.0
        # Boost up to +20 points for strong UOA
        disc_score = round(min(disc_score + uoa_strength * 20, 100), 1)

    record = {
        "ticker": ticker,
        "company": company,
        "sources": sources,
        # Insider fields — null since no Form 4 data
        "action": None,
        "role": None,
        "flags": None,
        "value_usd": None,
        "price_usd": uoa.get("price") if uoa else None,
        "qty": None,
        "insider": None,
        "trade_date": None,
        "filing_datetime": None,
        "filing_lag_days": None,
        "urgency_score": None,
        "urgency_signals": None,
        "sec_form4_url": None,
        # Trending data
        "trending": {
            "watchlist_count": trend.get("watchlist_count"),
            "sentiment": trend.get("sentiment"),
            "scanned_at": trend.get("scanned_at"),
        } if trend else None,
        # Discovery scoring
        "discovery_score": disc_score,
        "discovery_signals": disc_signals,
        # UOA data
        "uoa": {
            "uoa_score": uoa.get("uoa_score"),
            "unusual_contracts": uoa.get("unusual_contracts"),
            "total_dollar_volume": uoa.get("total_dollar_volume"),
            "max_vol_oi_ratio": uoa.get("max_vol_oi_ratio"),
            "dominant_expiry": uoa.get("dominant_expiry"),
            "call_put_ratio": uoa.get("call_put_ratio"),
            "otm_call_pct": uoa.get("otm_call_pct"),
            "top_contracts": uoa.get("top_contracts", [])[:3],
            "scanned_at": uoa.get("scanned_at"),
        } if uoa else None,
        # Enrichment placeholders — downstream agents fill these
        "options": None,
        "short_interest": None,
        "earnings": None,
        "sentiment": {**trend["sentiment"], "watchlist_count": trend.get("watchlist_count")} if trend and trend.get("sentiment") else None,
        "composite_score": None,
        "score_breakdown": None,
        "merged_at": merged_at,
    }
    return record


def main() -> int:
    insider_path = sys.argv[1] if len(sys.argv) > 1 else "trades_final.jsonl"
    trending_path = sys.argv[2] if len(sys.argv) > 2 else "trending.jsonl"
    uoa_path = sys.argv[3] if len(sys.argv) > 3 else "uoa.jsonl"
    out_path = sys.argv[4] if len(sys.argv) > 4 else "universe.jsonl"

    insiders = _load_jsonl(insider_path)
    trending = _load_jsonl(trending_path)
    uoa_records = _load_jsonl(uoa_path)

    # Index insider trades by ticker (may have multiple trades per ticker)
    insider_by_ticker: dict[str, list[dict]] = {}
    for trade in insiders:
        insider_by_ticker.setdefault(trade["ticker"], []).append(trade)

    # Sync insider tickers into watchlist for next UOA scan
    added = _sync_watchlist(set(insider_by_ticker))
    if added:
        print(f"  added {added} insider ticker(s) to {_WATCHLIST_PATH}", file=sys.stderr)

    # Index trending by ticker
    trending_by_ticker: dict[str, dict] = {}
    for t in trending:
        trending_by_ticker[t["ticker"]] = t

    # Index UOA by ticker
    uoa_by_ticker: dict[str, dict] = {}
    for u in uoa_records:
        uoa_by_ticker[u["ticker"]] = u

    all_tickers = set(insider_by_ticker) | set(trending_by_ticker) | set(uoa_by_ticker)

    # Build ticker -> company name map from insider records
    ticker_company: dict[str, str] = {}
    for trade in insiders:
        t = trade["ticker"]
        c = trade.get("company") or ""
        if t not in ticker_company and c:
            ticker_company[t] = c

    # Resolve missing company names for non-insider tickers via SEC
    missing = all_tickers - set(ticker_company)
    if missing:
        ticker_company.update(_fetch_company_names(missing))

    merged_at = datetime.now(timezone.utc).isoformat()

    results: list[dict] = []

    for ticker in sorted(all_tickers):
        insider_trades = insider_by_ticker.get(ticker)
        trend = trending_by_ticker.get(ticker)
        uoa = uoa_by_ticker.get(ticker)

        if insider_trades:
            for trade in insider_trades:
                record = dict(trade)
                record["sources"] = ["openinsider"]
                record["uoa"] = None

                if trend:
                    _attach_trending(record, trend)
                else:
                    record["trending"] = None

                if uoa:
                    _attach_uoa(record, uoa)

                record["merged_at"] = merged_at
                results.append(record)
        else:
            # Non-insider record (trending-only, UOA-only, or trending+UOA)
            sources: list[str] = []
            if trend:
                sources.append("stocktwits_trending")
            if uoa:
                sources.append("uoa_scan")

            record = _build_non_insider_record(
                ticker, sources, trend, uoa, merged_at,
                company=ticker_company.get(ticker, ""),
            )
            results.append(record)

    # --- Unified sort ---
    # Insider trades first (by composite desc), then non-insider (by best available score)
    def _sort_key(r: dict) -> tuple:
        has_insider = 1 if "openinsider" in r.get("sources", []) else 0
        composite = r.get("composite_score") or 0
        discovery = r.get("discovery_score") or 0
        uoa_s = (r.get("uoa") or {}).get("uoa_score") or 0
        # For non-insider: use max of discovery and uoa_score
        best_alt = max(discovery, uoa_s)
        return (has_insider, composite, best_alt)

    results.sort(key=_sort_key, reverse=True)

    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    insider_count = sum(1 for r in results if "openinsider" in r.get("sources", []))
    trending_only = sum(1 for r in results if r.get("sources") == ["stocktwits_trending"])
    uoa_only = sum(1 for r in results if r.get("sources") == ["uoa_scan"])
    multi = sum(1 for r in results if len(r.get("sources", [])) > 1)

    print(
        f"merged {len(results)} records "
        f"({insider_count} insider, {trending_only} trending-only, "
        f"{uoa_only} uoa-only, {multi} multi-source) "
        f"-> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
