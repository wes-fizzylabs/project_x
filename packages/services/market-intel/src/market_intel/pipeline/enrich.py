"""Post-merge enrichment for non-insider universe records.

Reads universe.jsonl and enriches any record missing options/short_interest/
earnings/sentiment data (i.e., UOA-only and trending-only tickers that skipped
the insider enrichment pipeline).

Uses the same Yahoo Finance and StockTwits functions as the insider pipeline
to ensure consistent data quality across all sources.

Usage (standalone):
    python -m market_intel.pipeline.enrich                          # defaults
    python -m market_intel.pipeline.enrich universe.jsonl out.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date

from market_intel.pipeline.yahoo import (
    _options_multiplier,
    _short_squeeze_multiplier,
    collect_ticker_data,
)
from market_intel.interpret import add_labels
from market_intel.pipeline.sentiment import _sentiment_multiplier, fetch_sentiment


def _base_score(record: dict) -> tuple[float, str]:
    """Pick the best available base score for a non-insider record.

    Returns (score, source_label) where score is on a 0–100 scale
    comparable to urgency_score.
    """
    uoa_score = (record.get("uoa") or {}).get("uoa_score") or 0
    discovery_score = record.get("discovery_score") or 0
    if uoa_score >= discovery_score:
        return uoa_score, "uoa_score"
    return discovery_score, "discovery_score"


# Field order that puts identification and labels first for agent readability
_FIELD_ORDER = [
    # --- identity ---
    "ticker", "company", "sector", "industry", "sources", "price_usd",
    # --- top-level labels (the "so what") ---
    "composite_score", "composite_label",
    "urgency_score", "urgency_label",
    "discovery_score", "discovery_label",
    "uoa_label",
    "institutional_label",
    # --- score details ---
    "score_breakdown",
    # --- insider-specific ---
    "action", "role", "flags", "value_usd", "qty",
    "delta_own_pct", "owned_after",
    "insider", "insider_titles", "co_filers",
    "trade_date", "filing_datetime", "filing_lag_days",
    "urgency_signals",
    # --- market data ---
    "options", "short_interest", "earnings", "sentiment",
    # --- source detail ---
    "uoa", "trending", "institutional",
    # --- meta ---
    "sec_form4_url", "merged_at",
]


def _reorder(record: dict) -> dict:
    """Reorder record fields for agent readability — labels first, raw data last."""
    ordered: dict = {}
    for key in _FIELD_ORDER:
        if key in record:
            ordered[key] = record[key]
    # Append any remaining fields not in the explicit order
    for key, val in record.items():
        if key not in ordered:
            ordered[key] = val
    return ordered


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

    today_str = date.today().isoformat()

    yahoo_cache: dict[str, dict] = {}
    sentiment_cache: dict[str, dict] = {}
    enriched_count = 0

    for record in records:
        # Skip records already enriched (insider pipeline)
        if record.get("options") is not None:
            continue

        ticker = record["ticker"]
        price_usd = record.get("price_usd")

        # --- Yahoo Finance: options, short interest, earnings ---
        if ticker not in yahoo_cache:
            print(f"  enriching: {ticker}", file=sys.stderr)
            yahoo_cache[ticker] = collect_ticker_data(
                ticker, price_usd, today_str,
            )
        yahoo_data = yahoo_cache[ticker]
        record["options"] = yahoo_data["options"]
        record["short_interest"] = yahoo_data["short_interest"]
        record["earnings"] = yahoo_data["earnings"]

        # Backfill price if missing (trending-only / UOA-only records)
        if not record.get("price_usd") and yahoo_data.get("price"):
            record["price_usd"] = yahoo_data["price"]

        # Flatten sector/industry to top-level (skip if already present)
        sec = yahoo_data["sector"]
        if sec.get("available") and not record.get("sector"):
            record["sector"] = sec["sector"]
            record["industry"] = sec["industry"]

        # --- Sentiment ---
        if ticker not in sentiment_cache:
            print(f"  fetching sentiment: {ticker}", file=sys.stderr)
            sentiment_cache[ticker] = fetch_sentiment(ticker)
            time.sleep(0.3)
        # Only overwrite if record has no sentiment or it's incomplete
        existing_sent = record.get("sentiment") or {}
        if not existing_sent.get("available"):
            record["sentiment"] = sentiment_cache[ticker]

        # --- Composite score ---
        base, base_source = _base_score(record)
        opt_mult = _options_multiplier(record["options"], price_usd)
        si_mult = _short_squeeze_multiplier(record["short_interest"])
        sent_mult = _sentiment_multiplier(record.get("sentiment") or {})

        composite = base * opt_mult * si_mult * sent_mult
        record["composite_score"] = round(composite, 1)
        record["score_breakdown"] = {
            "base": base,
            "base_source": base_source,
            "multipliers": {
                "options": opt_mult,
                "short_squeeze": si_mult,
                "sentiment": sent_mult,
            },
        }
        enriched_count += 1

    # Re-sort using same logic as merge_sources.py
    def _sort_key(r: dict) -> tuple:
        has_insider = 1 if "openinsider" in r.get("sources", []) else 0
        composite = r.get("composite_score") or 0
        discovery = r.get("discovery_score") or 0
        uoa_s = (r.get("uoa") or {}).get("uoa_score") or 0
        best_alt = max(discovery, uoa_s)
        return (has_insider, composite, best_alt)

    records.sort(key=_sort_key, reverse=True)

    # Add interpretation labels to ALL records (insider + non-insider)
    for record in records:
        add_labels(record)

    # Reorder fields so labels & scores are near the top for agent consumption
    records = [_reorder(r) for r in records]

    # Atomic write — single-line JSONL for pipeline compatibility
    with open(tmp_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_path, out_path)

    # Agent-facing formatted output
    agent_path = out_path.replace(".jsonl", ".json")
    with open(agent_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(
        f"enriched {enriched_count} non-insider records -> {out_path}, {agent_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
