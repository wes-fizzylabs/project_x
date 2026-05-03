"""Enrich trades with StockTwits sentiment and recompute composite scores.

Reads trades_enriched.jsonl (post-options), fetches sentiment for each
ticker from the StockTwits API, applies a sentiment multiplier, and
writes trades_final.jsonl with updated composite scores.

Auth: set STOCKTWITS_USER and STOCKTWITS_PASS env vars before running.

Usage (standalone):
    python -m market_intel.pipeline.sentiment                                    # defaults
    python -m market_intel.pipeline.sentiment enriched.jsonl final.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

_API_BASE = "https://api.stocktwits.com/api/2"
_USER = os.environ.get("STOCKTWITS_USER", "")
_PASS = os.environ.get("STOCKTWITS_PASS", "")


def _auth() -> tuple[str, str] | None:
    if _USER and _PASS:
        return (_USER, _PASS)
    return None


def fetch_sentiment(ticker: str) -> dict:
    """Fetch sentiment from two StockTwits endpoints:

    1. /symbols/{ticker}/sentiment.json — daily bullish/bearish percentages
    2. /streams/symbol/{ticker}.json  — recent messages for volume + watchlist count

    Returns a normalised dict combining both signals.
    """
    auth = _auth()
    result: dict = {"available": False}

    # --- daily sentiment percentages ---
    try:
        resp = httpx.get(
            f"{_API_BASE}/symbols/{ticker}/sentiment.json",
            auth=auth, timeout=10.0,
        )
        resp.raise_for_status()
        days = resp.json().get("data", [])
        if days:
            latest = days[0]
            result["bullish_pct"] = float(latest.get("bullish", 0))
            result["bearish_pct"] = float(latest.get("bearish", 0))
            result["available"] = True
    except Exception as e:
        print(f"  sentiment/sentiment error {ticker}: {e}", file=sys.stderr)

    # --- message stream for volume + watchlist ---
    try:
        resp = httpx.get(
            f"{_API_BASE}/streams/symbol/{ticker}.json",
            auth=auth, timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

        sym = data.get("symbol", {})
        result["watchlist_count"] = sym.get("watchlist_count", 0)

        msgs = data.get("messages", [])
        bull = bear = 0
        for m in msgs:
            basic = ((m.get("entities") or {}).get("sentiment") or {}).get("basic", "")
            if basic == "Bullish":
                bull += 1
            elif basic == "Bearish":
                bear += 1
        result["recent_messages"] = len(msgs)
        result["recent_bullish"] = bull
        result["recent_bearish"] = bear
        result["available"] = True
    except Exception as e:
        print(f"  sentiment/stream error {ticker}: {e}", file=sys.stderr)

    return result


def _sentiment_multiplier(sentiment: dict) -> float:
    """Compute a 0.6–1.0 multiplier where quiet/neutral is best.

    Core idea: for an insider-buying strategy, you WANT to be early.
    Low chatter = nobody's watching = ideal entry.
    High chatter + very bullish = crowd is already in = edge is thinner.
    Bearish sentiment = contrarian — treat as roughly neutral.
    """
    if not sentiment.get("available"):
        return 1.0  # no data = nobody's talking = best case

    bull_pct = sentiment.get("bullish_pct", 50) or 50
    watchlist = sentiment.get("watchlist_count", 0) or 0

    # --- attention factor: based on watchlist size ---
    # Watchlist count is a stable proxy for how many eyes are on this ticker
    if watchlist < 5_000:
        att_factor = 1.0   # under the radar — ideal
    elif watchlist < 25_000:
        att_factor = 0.95  # some following
    elif watchlist < 100_000:
        att_factor = 0.9   # well-known
    else:
        att_factor = 0.8   # heavily watched

    # --- polarity factor: bullish hype erodes your edge ---
    if bull_pct >= 80:
        pol_factor = 0.75  # extreme hype — IV likely elevated, crowd is in
    elif bull_pct >= 65:
        pol_factor = 0.85  # bullish consensus building
    elif bull_pct >= 40:
        pol_factor = 1.0   # neutral/mixed — no crowd effect
    else:
        pol_factor = 0.9   # bearish — contrarian play, slight caution

    return round(att_factor * pol_factor, 2)


def main() -> int:
    in_path = sys.argv[1] if len(sys.argv) > 1 else "trades_enriched.jsonl"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "trades_final.jsonl"

    if not _USER or not _PASS:
        print("warning: STOCKTWITS_USER/STOCKTWITS_PASS not set, sentiment will be unavailable",
              file=sys.stderr)

    trades: list[dict] = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            trades.append(json.loads(line))

    seen: dict[str, dict] = {}
    for trade in trades:
        ticker = trade["ticker"]
        if ticker not in seen:
            print(f"  fetching sentiment: {ticker}", file=sys.stderr)
            seen[ticker] = fetch_sentiment(ticker)
            time.sleep(0.3)  # basic rate-limit courtesy
        trade["sentiment"] = seen[ticker]

        # --- recompute composite with sentiment multiplier ---
        sent_mult = _sentiment_multiplier(trade["sentiment"])
        urgency = trade.get("urgency_score", 0)

        # Preserve existing multipliers, add sentiment
        breakdown = trade.get("score_breakdown", {"urgency": urgency, "multipliers": {}})
        multipliers = breakdown.get("multipliers", {})
        multipliers["sentiment"] = sent_mult

        # Composite = urgency × product of all multipliers
        composite = urgency
        for m in multipliers.values():
            composite *= m
        composite = round(composite, 1)

        trade["composite_score"] = composite
        trade["score_breakdown"] = {
            "urgency": urgency,
            "multipliers": multipliers,
        }

    # Final ordering
    trades.sort(
        key=lambda t: (t["composite_score"], t.get("urgency_score", 0)),
        reverse=True,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    no_sent = sum(1 for t in trades if not t["sentiment"]["available"])
    print(
        f"sentiment-enriched {len(trades)} trades ({no_sent} without data) -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
