"""Read raw trades.jsonl, score urgency, emit trades_shaped.jsonl for the next agent.

Usage (standalone):
    python -m market_intel.pipeline.shape                         # trades.jsonl -> trades_shaped.jsonl
    python -m market_intel.pipeline.shape in.jsonl out.jsonl
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from market_intel.models import InsiderTrade


# ---------------------------------------------------------------------------
# Co-filing dedup
# ---------------------------------------------------------------------------

_ROLE_PRIORITY = {"executive": 0, "director": 1, "major_holder": 2, "other": 3}


def _dedup_co_filings(trades: list[InsiderTrade]) -> list[InsiderTrade]:
    """Merge co-filings of the same transaction into a single record.

    Related parties (e.g. a fund + its principal) often file separate Form 4s
    for the same transaction — identical ticker, date, price, and quantity.
    We merge these into one record, keeping the earliest filing and storing
    additional filer names in co_filers.
    """
    groups: dict[tuple, list[InsiderTrade]] = defaultdict(list)
    for t in trades:
        key = (t.ticker, t.trade_date.isoformat(), t.price_usd, t.qty)
        groups[key].append(t)

    result: list[InsiderTrade] = []
    merged_count = 0
    for group in groups.values():
        if len(group) == 1:
            result.append(group[0])
            continue

        # Earliest filing first
        group.sort(key=lambda t: t.filing_datetime)
        primary = group[0]
        others = group[1:]

        # Combine roles from all filers, pick highest-signal category
        all_roles = list({r for t in group for r in t.insider_roles})
        best_category = min(
            (t.role_category for t in group),
            key=lambda c: _ROLE_PRIORITY.get(c, 99),
        )
        # Combine unique flags
        all_flags = "".join(sorted({c for t in group for c in (t.flags_raw or "")}))

        merged = primary.model_copy(update={
            "insider_roles": all_roles,
            "role_category": best_category,
            "flags_raw": all_flags,
            "co_filers": [t.insider_name for t in others],
        })
        result.append(merged)
        merged_count += len(others)

    if merged_count:
        print(f"deduped {merged_count} co-filing(s)", file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Cluster detection
# ---------------------------------------------------------------------------

def _cluster_counts(trades: list[InsiderTrade]) -> dict[str, int]:
    """Count distinct insiders per ticker across the full dataset.

    Because the scraper URL already limits results to a 3-day window,
    every trade in the dataset for a given ticker falls within the
    cluster window — no rolling-date logic needed.
    """
    ticker_insiders: dict[str, set[str]] = defaultdict(set)
    for t in trades:
        ticker_insiders[t.ticker].add(t.insider_name)
    return {ticker: len(names) for ticker, names in ticker_insiders.items()}


def _cluster_score(count: int) -> int:
    """0–24 pts. Multiple distinct insiders buying = pack behaviour."""
    if count >= 3:
        return 24
    if count == 2:
        return 14
    return 0


# ---------------------------------------------------------------------------
# Composite urgency
# ---------------------------------------------------------------------------

def _compute_urgency(trade: InsiderTrade, cluster_count: int) -> dict:
    """Return urgency_score (0-100) and per-signal breakdown.

    The raw score (sum of 5 sub-signals) is multiplied by flag_quality
    to penalise option exercises (M) and amended filings (A).
    """
    signals = {
        "filing_lag": trade.filing_lag_score(),
        "role": trade.role_score(),
        "size": trade.size_score(),
        "cluster": _cluster_score(cluster_count),
        "price": trade.price_score(),
    }
    raw = sum(signals.values())
    flag_mult = trade.flag_quality()
    return {
        "urgency_score": round(raw * flag_mult),
        "urgency_signals": {
            **signals,
            "cluster_count": cluster_count,
            "flag_quality": flag_mult,
        },
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    in_path = sys.argv[1] if len(sys.argv) > 1 else "trades.jsonl"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "trades_shaped.jsonl"

    # --- parse ---
    trades: list[InsiderTrade] = []
    skipped = 0
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            try:
                trade = InsiderTrade.from_raw(raw)
            except Exception as e:
                skipped += 1
                print(f"skip {raw.get('ticker', '?')}: {e}", file=sys.stderr)
                continue
            trades.append(trade)

    # --- dedup co-filings before cluster detection ---
    trades = _dedup_co_filings(trades)

    # --- score ---
    clusters = _cluster_counts(trades)

    shaped: list[dict] = []
    for trade in trades:
        payload = trade.to_agent_payload()
        urgency = _compute_urgency(trade, clusters[trade.ticker])
        payload["urgency_score"] = urgency["urgency_score"]
        payload["urgency_signals"] = urgency["urgency_signals"]
        shaped.append(payload)

    # Sort by urgency score desc; tie-break on dollar size desc.
    shaped.sort(
        key=lambda t: (t["urgency_score"], abs(t["value_usd"] or 0)),
        reverse=True,
    )

    # --- write ---
    with open(out_path, "w", encoding="utf-8") as f:
        for t in shaped:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    print(f"shaped {len(shaped)} trades ({skipped} skipped) -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
