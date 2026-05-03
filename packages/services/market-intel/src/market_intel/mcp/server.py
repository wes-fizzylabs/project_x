"""Market Intel — MCP Server.

Exposes the pipeline's universe data and on-demand research tools as MCP tools
for downstream research agents. Does NOT replace the batch pipeline.

Usage:
    # Run via uv entrypoint:
    uv run market-intel-mcp

    # Or via fastmcp CLI:
    uv run fastmcp run market_intel/mcp/server.py
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from fastmcp import FastMCP

from market_intel import DATA_DIR
from market_intel.pipeline.yahoo import collect_ticker_data
from market_intel.interpret import add_labels
from market_intel.pipeline.sentiment import fetch_sentiment

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

_UNIVERSE_PATH = DATA_DIR / "universe.json"
_PIPELINE_LOCK = DATA_DIR / ".pipeline.lock"
_PIPELINE_COOLDOWN = 3600  # seconds — 1 hour between runs

mcp = FastMCP(
    "Open Insider Research",
    instructions=(
        "Tools for researching insider trades, unusual options activity, "
        "and trending equities. Data comes from the daily pipeline output "
        "(universe.json). Use get_universe to read the latest scan, "
        "and interpret_record to generate human-readable labels for any record. "
        "Use market_snapshot and get_sentiment to gather live data on any ticker "
        "— including tickers NOT in today's universe."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_universe() -> list[dict]:
    """Load universe.json (the agent-facing formatted output)."""
    if not _UNIVERSE_PATH.exists():
        return []
    with open(_UNIVERSE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _check_pipeline_cooldown() -> tuple[bool, float]:
    """Check if the pipeline lockfile allows a new run.

    Returns (can_run, seconds_remaining).
    """
    if not _PIPELINE_LOCK.exists():
        return True, 0
    try:
        lock_time = float(_PIPELINE_LOCK.read_text().strip())
    except (ValueError, OSError):
        return True, 0
    elapsed = time.time() - lock_time
    remaining = _PIPELINE_COOLDOWN - elapsed
    if remaining <= 0:
        return True, 0
    return False, remaining


# ---------------------------------------------------------------------------
# Tools — universe & interpretation (read-only, from pipeline output)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_universe(ticker: str | None = None) -> list[dict]:
    """Read the latest pipeline universe, optionally filtered to a single ticker.

    Returns the full list of scored records from the most recent pipeline run.
    Each record includes insider trades, options data, short interest, earnings,
    sentiment, UOA signals, and composite scores.

    Args:
        ticker: Optional ticker symbol to filter to (e.g. "AVLN"). Returns all
                records if omitted.
    """
    records = _load_universe()
    if ticker:
        upper = ticker.upper()
        records = [r for r in records if r.get("ticker", "").upper() == upper]
    return records


@mcp.tool()
def interpret_record(record: dict) -> dict:
    """Add human-readable interpretation labels to a universe record.

    Takes a raw or partially-labeled record and adds/updates composite_label,
    urgency_label, institutional_label, and multiplier explanation labels.
    Useful for re-interpreting records after manual score adjustments.

    Args:
        record: A universe record dict (as returned by get_universe).

    Returns:
        The same record dict with interpretation labels added/updated.
    """
    add_labels(record)
    return record


# ---------------------------------------------------------------------------
# Tools — live data (on-demand, hits external APIs)
# ---------------------------------------------------------------------------

@mcp.tool()
def market_snapshot(ticker: str, trade_date: str | None = None) -> dict:
    """Fetch live market data for any ticker: options, short interest, earnings, sector.

    Use this to gather hard financial data on a ticker — whether it came from
    the universe or the researcher discovered it independently. Returns current
    price, options chain liquidity (ATM spread, OI, IV), bucketed expiries
    (1m/3m/6m), short interest (% float, days to cover), earnings timing, and
    sector/industry classification.

    This hits the Yahoo Finance API live — use judiciously.

    Args:
        ticker: Stock ticker symbol (e.g. "AVLN", "MRNA").
        trade_date: Reference date for earnings alignment (ISO format,
                    e.g. "2026-05-01"). Defaults to today if omitted.

    Returns:
        Dict with keys: options, short_interest, sector, earnings, price.
    """
    ref_date = trade_date or date.today().isoformat()
    data = collect_ticker_data(ticker.upper(), price_usd=None, trade_date=ref_date)
    # If collect_ticker_data resolved a live price, inject it so options
    # liquidity context makes sense alongside the price.
    if data.get("price"):
        data["price_usd"] = data["price"]
    return data


@mcp.tool()
def get_sentiment(ticker: str) -> dict:
    """Fetch StockTwits sentiment and crowd-awareness data for any ticker.

    Returns watchlist count (how many eyes are on this ticker), recent message
    volume with bullish/bearish breakdown, and daily sentiment percentages.

    For an insider-buying strategy, LOW chatter is ideal — it means you're
    early and the crowd hasn't found the trade yet. High bullish hype suggests
    the edge is thinner and IV may be elevated.

    This hits the StockTwits API live — use judiciously.

    Args:
        ticker: Stock ticker symbol (e.g. "AVLN", "MRNA").

    Returns:
        Dict with keys: available, watchlist_count, recent_messages,
        recent_bullish, recent_bearish, bullish_pct, bearish_pct.
    """
    return fetch_sentiment(ticker.upper())


# ---------------------------------------------------------------------------
# Tools — pipeline execution
# ---------------------------------------------------------------------------

@mcp.tool()
def run_full_pipeline(force: bool = False) -> dict:
    """Run the full 8-stage data pipeline (scrape, enrich, score, merge).

    Executes the pipeline stages which scrape OpenInsider, enrich with Yahoo
    Finance (options, SI, earnings), fetch StockTwits sentiment and trending,
    scan for unusual options activity, look up 13F institutional holders,
    and produce the scored universe.

    A 1-hour cooldown lockfile prevents accidental re-runs. Use force=True
    to bypass the cooldown (e.g. if the previous run failed).

    Call this once at the start of a research session, then use get_universe
    to read the results.

    Args:
        force: Bypass the 1-hour cooldown (default False).

    Returns:
        Dict with keys: status, message, duration_seconds, universe_records.
    """
    can_run, remaining = _check_pipeline_cooldown()
    if not can_run and not force:
        mins = int(remaining // 60)
        return {
            "status": "cooldown",
            "message": f"Pipeline ran recently. {mins}m remaining. Use force=True to override.",
            "seconds_remaining": round(remaining),
        }

    # Ensure data directory exists
    _UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Write lockfile before starting
    _PIPELINE_LOCK.write_text(str(time.time()))

    start = time.time()
    try:
        from market_intel.pipeline.run import run_pipeline
        run_pipeline(data_dir=str(_UNIVERSE_PATH.parent))
    except Exception as exc:
        _PIPELINE_LOCK.unlink(missing_ok=True)
        return {
            "status": "error",
            "message": f"Pipeline failed: {exc}",
        }

    duration = round(time.time() - start, 1)

    # Count records in the fresh universe
    records = _load_universe()

    return {
        "status": "success",
        "message": f"Pipeline completed in {duration}s. {len(records)} records in universe.",
        "duration_seconds": duration,
        "universe_records": len(records),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()
