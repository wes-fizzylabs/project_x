"""Python pipeline orchestrator — replaces run_pipeline.sh.

Runs all 8 pipeline stages sequentially, writing intermediate JSONL files
to a configurable data directory.

Usage:
    uv run market-intel-pipeline              # uses package-relative data/ dir
    uv run market-intel-pipeline /path/to/dir # custom data directory
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def run_pipeline(data_dir: str | None = None) -> None:
    """Execute the full 8-stage pipeline.

    Args:
        data_dir: Directory for intermediate and output files.
                  Defaults to the package-relative data/ directory.
    """
    from market_intel import DATA_DIR
    d = Path(data_dir) if data_dir else DATA_DIR
    d.mkdir(parents=True, exist_ok=True)

    trades = d / "trades.jsonl"
    shaped = d / "trades_shaped.jsonl"
    enriched = d / "trades_enriched.jsonl"
    final = d / "trades_final.jsonl"
    trending_f = d / "trending.jsonl"
    uoa_f = d / "uoa.jsonl"
    universe = d / "universe.jsonl"
    watchlist = d / "watchlist.txt"

    # Ensure watchlist exists
    if not watchlist.exists():
        watchlist.write_text("# Watchlist — one ticker per line\n")

    # Clean intermediate files
    print("=== Cleaning intermediate files ===", file=sys.stderr)
    for f in [trades, shaped, enriched, final, trending_f, uoa_f]:
        f.unlink(missing_ok=True)

    # Stage 1: Scrape OpenInsider
    print("=== 1/8  Scraping OpenInsider ===", file=sys.stderr)
    from market_intel.pipeline.scrape import fetch, parse
    from datetime import datetime, timezone
    html = fetch()
    rows = parse(html)
    scraped_at = datetime.now(timezone.utc).isoformat()
    with open(trades, "w", encoding="utf-8") as fp:
        for row in rows:
            row["scraped_at"] = scraped_at
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  wrote {len(rows)} trades", file=sys.stderr)

    # Stage 2: Shape trades
    print("=== 2/8  Shaping trades ===", file=sys.stderr)
    sys.argv = ["shape", str(trades), str(shaped)]
    from market_intel.pipeline.shape import main as shape_main
    shape_main()

    # Stage 3: Enrich with Yahoo Finance
    print("=== 3/8  Enriching (Yahoo Finance) ===", file=sys.stderr)
    sys.argv = ["yahoo", str(shaped), str(enriched)]
    from market_intel.pipeline.yahoo import main as yahoo_main
    yahoo_main()

    # Stage 4: Sentiment
    print("=== 4/8  Sentiment (StockTwits) ===", file=sys.stderr)
    sys.argv = ["sentiment", str(enriched), str(final)]
    from market_intel.pipeline.sentiment import main as sentiment_main
    sentiment_main()

    # Stage 5: Trending
    print("=== 5/8  Trending equities (StockTwits) ===", file=sys.stderr)
    sys.argv = ["trending", str(trending_f)]
    from market_intel.pipeline.trending import main as trending_main
    trending_main()

    # Stage 6: UOA scan + merge
    print("=== 6/8  UOA scan + merge ===", file=sys.stderr)
    sys.argv = ["uoa", str(universe), str(watchlist), str(uoa_f)]
    from market_intel.pipeline.uoa import main as uoa_main
    uoa_main()

    sys.argv = ["merge", str(final), str(trending_f), str(uoa_f), str(universe)]
    from market_intel.pipeline.merge import main as merge_main
    merge_main()

    # Stage 7: 13F institutional holders
    print("=== 7/8  13F institutional holders (SEC EDGAR) ===", file=sys.stderr)
    sys.argv = ["sec13f", str(universe), str(universe)]
    from market_intel.pipeline.sec13f import main as sec13f_main
    sec13f_main()

    # Stage 8: Enrich non-insider records
    print("=== 8/8  Enriching non-insider universe records ===", file=sys.stderr)
    sys.argv = ["enrich", str(universe), str(universe)]
    from market_intel.pipeline.enrich import main as enrich_main
    enrich_main()

    # Summary
    records = []
    with open(universe, encoding="utf-8") as fp:
        for line in fp:
            if line.strip():
                records.append(json.loads(line))
    src_counts: dict[str, int] = {}
    for r in records:
        key = "+".join(sorted(r.get("sources", [])))
        src_counts[key] = src_counts.get(key, 0) + 1
    print(f"\n=== Done. {len(records)} tickers in universe ===", file=sys.stderr)
    for k, v in sorted(src_counts.items()):
        print(f"  {k}: {v}", file=sys.stderr)


def main() -> int:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        run_pipeline(data_dir)
    except Exception as exc:
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
