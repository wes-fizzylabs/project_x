"""Narrative intelligence pipeline runner.

Runs all pipeline stages sequentially, writing intermediate JSONL files
to a configurable data directory.

Stages:
  1. Reddit sentiment + discovery
  2. Macro economic calendar (FOMC, CPI, NFP)
  3. (future) Political trades (Congress/Senate STOCK Act)
  4. (future) SEC 8-K material events
  5. (future) News headlines
  6. Merge into narrative.json

Usage:
    uv run narrative-intel-pipeline              # uses package-relative data/ dir
    uv run narrative-intel-pipeline /path/to/dir # custom data directory
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def run_pipeline(data_dir: str | None = None) -> None:
    """Execute the narrative intelligence pipeline."""
    from narrative_intel import DATA_DIR
    d = Path(data_dir) if data_dir else DATA_DIR
    d.mkdir(parents=True, exist_ok=True)

    reddit_f = d / "reddit.jsonl"
    macro_f = d / "macro.jsonl"

    # Clean intermediate files
    print("=== Cleaning intermediate files ===", file=sys.stderr)
    for f in [reddit_f, macro_f]:
        f.unlink(missing_ok=True)

    # Stage 1: Reddit
    print("=== 1/2  Reddit sentiment + discovery ===", file=sys.stderr)
    from narrative_intel.pipeline.reddit import run as reddit_run
    reddit_run(str(reddit_f))

    # Stage 2: Macro calendar
    print("=== 2/2  Macro economic calendar ===", file=sys.stderr)
    from narrative_intel.pipeline.macro import run as macro_run
    macro_run(str(macro_f))

    # --- Merge into narrative.json ---
    print("=== Merging into narrative.json ===", file=sys.stderr)
    all_records: list[dict] = []

    # Load Reddit records
    if reddit_f.exists():
        with open(reddit_f, encoding="utf-8") as fp:
            for line in fp:
                if line.strip():
                    all_records.append(json.loads(line))

    # Load macro calendar records
    if macro_f.exists():
        with open(macro_f, encoding="utf-8") as fp:
            for line in fp:
                if line.strip():
                    all_records.append(json.loads(line))

    # (future stages append here)

    # Sort by mentions descending
    all_records.sort(key=lambda r: r.get("mentions", 0), reverse=True)

    # Write merged output
    narrative_path = d / "narrative.json"
    with open(narrative_path, "w", encoding="utf-8") as fp:
        json.dump(all_records, fp, indent=2, ensure_ascii=False)

    print(
        f"\n=== Done. {len(all_records)} narrative records ===",
        file=sys.stderr,
    )
    for source in sorted({r.get("source", "unknown") for r in all_records}):
        count = sum(1 for r in all_records if r.get("source") == source)
        print(f"  {source}: {count}", file=sys.stderr)


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
