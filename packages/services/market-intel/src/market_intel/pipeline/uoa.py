"""Scan for Unusual Options Activity (UOA) across universe + watchlist tickers.

Pulls options chains via yfinance, flags contracts where volume significantly
exceeds open interest (new positions being opened aggressively), and scores
each ticker by how unusual the activity is.

Input tickers come from two sources:
  1. universe.jsonl — tickers already in the pipeline
  2. watchlist.txt  — user-configurable list (one ticker per line)

Outputs uoa.jsonl — one record per ticker with unusual activity detected.

Usage (standalone):
    python -m market_intel.pipeline.uoa                                           # defaults
    python -m market_intel.pipeline.uoa universe.jsonl watchlist.txt uoa.jsonl
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone

import yfinance as yf


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

_VOL_OI_THRESHOLD = 3.0    # volume/OI ratio to flag a contract as unusual
_MIN_VOLUME = 100           # ignore contracts with trivial volume
_MAX_EXPIRATIONS = 6        # scan up to this many expirations per ticker


# ---------------------------------------------------------------------------
# Ticker list assembly
# ---------------------------------------------------------------------------

def _load_universe_tickers(path: str) -> set[str]:
    tickers: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    t = rec.get("ticker")
                    if t:
                        tickers.add(t)
    except FileNotFoundError:
        print(f"  warning: {path} not found, skipping", file=sys.stderr)
    return tickers


def _load_watchlist(path: str) -> set[str]:
    tickers: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    tickers.add(line.upper())
    except FileNotFoundError:
        print(f"  warning: {path} not found, skipping", file=sys.stderr)
    return tickers


# ---------------------------------------------------------------------------
# Chain scanning
# ---------------------------------------------------------------------------

def _scan_chain(ticker: str) -> dict | None:
    """Scan a ticker's options chain for unusual activity.

    Returns a record dict if unusual activity found, else None.
    """
    try:
        t = yf.Ticker(ticker)
        expirations = t.options
    except Exception:
        return None

    if not expirations:
        return None

    today = date.today()
    unusual_contracts: list[dict] = []
    total_unusual_volume = 0
    total_unusual_dollar_volume = 0.0
    expiry_volume: dict[str, int] = {}  # track volume per expiry

    # Get current price for OTM/ITM classification
    try:
        price = t.info.get("currentPrice") or t.info.get("regularMarketPrice") or 0
    except Exception:
        price = 0

    for exp in expirations[:_MAX_EXPIRATIONS]:
        try:
            chain = t.option_chain(exp)
        except Exception:
            continue

        for side, df in [("call", chain.calls), ("put", chain.puts)]:
            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                raw_vol = row.get("volume", 0)
                raw_oi = row.get("openInterest", 0)
                vol = 0 if (raw_vol is None or (isinstance(raw_vol, float) and math.isnan(raw_vol))) else int(raw_vol)
                oi = 0 if (raw_oi is None or (isinstance(raw_oi, float) and math.isnan(raw_oi))) else int(raw_oi)
                raw_strike = row.get("strike", 0)
                strike = 0.0 if (raw_strike is None or (isinstance(raw_strike, float) and math.isnan(raw_strike))) else float(raw_strike)
                raw_bid = row.get("bid", 0)
                bid = 0.0 if (raw_bid is None or (isinstance(raw_bid, float) and math.isnan(raw_bid))) else float(raw_bid)
                raw_ask = row.get("ask", 0)
                ask = 0.0 if (raw_ask is None or (isinstance(raw_ask, float) and math.isnan(raw_ask))) else float(raw_ask)

                if vol < _MIN_VOLUME:
                    continue

                # Volume/OI ratio — core UOA signal
                if oi > 0:
                    ratio = vol / oi
                else:
                    # Volume with zero OI = brand new positions, very unusual
                    ratio = vol / 1.0  # treat as if OI=1

                if ratio < _VOL_OI_THRESHOLD:
                    continue

                # This contract is unusual
                mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
                dollar_vol = vol * mid * 100  # each contract = 100 shares

                # OTM classification
                if side == "call":
                    otm = strike > price if price > 0 else False
                else:
                    otm = strike < price if price > 0 else False

                contract = {
                    "expiry": exp,
                    "strike": strike,
                    "side": side,
                    "volume": vol,
                    "open_interest": oi,
                    "vol_oi_ratio": round(ratio, 1),
                    "bid": bid,
                    "ask": ask,
                    "mid": round(mid, 2),
                    "dollar_volume": round(dollar_vol),
                    "otm": otm,
                }
                unusual_contracts.append(contract)
                total_unusual_volume += vol
                total_unusual_dollar_volume += dollar_vol
                expiry_volume[exp] = expiry_volume.get(exp, 0) + vol

    if not unusual_contracts:
        return None

    # --- Aggregate signals ---
    call_contracts = [c for c in unusual_contracts if c["side"] == "call"]
    put_contracts = [c for c in unusual_contracts if c["side"] == "put"]
    otm_call_vol = sum(c["volume"] for c in call_contracts if c["otm"])
    total_call_vol = sum(c["volume"] for c in call_contracts)

    # Dominant expiry — where is most of the unusual volume concentrated?
    dominant_expiry = max(expiry_volume, key=expiry_volume.get) if expiry_volume else None
    dominant_pct = (
        round(expiry_volume[dominant_expiry] / total_unusual_volume * 100, 1)
        if dominant_expiry and total_unusual_volume > 0
        else 0
    )

    max_ratio = max(c["vol_oi_ratio"] for c in unusual_contracts)

    # --- UOA Score (0–100) ---
    score = _uoa_score(
        max_ratio=max_ratio,
        total_dollar_volume=total_unusual_dollar_volume,
        dominant_pct=dominant_pct,
        otm_call_vol=otm_call_vol,
        total_call_vol=total_call_vol,
        num_contracts=len(unusual_contracts),
    )

    # Top 5 most unusual contracts for the report
    top_contracts = sorted(unusual_contracts, key=lambda c: c["vol_oi_ratio"], reverse=True)[:5]

    return {
        "ticker": ticker,
        "source": "uoa_scan",
        "price": round(price, 2) if price else None,
        "unusual_contracts": len(unusual_contracts),
        "call_contracts": len(call_contracts),
        "put_contracts": len(put_contracts),
        "total_unusual_volume": total_unusual_volume,
        "total_dollar_volume": round(total_unusual_dollar_volume),
        "max_vol_oi_ratio": max_ratio,
        "dominant_expiry": dominant_expiry,
        "dominant_expiry_pct": dominant_pct,
        "call_put_ratio": (
            round(len(call_contracts) / len(put_contracts), 2)
            if put_contracts else None
        ),
        "otm_call_pct": (
            round(otm_call_vol / total_call_vol * 100, 1)
            if total_call_vol > 0 else 0
        ),
        "uoa_score": score,
        "uoa_signals": {
            "ratio_score": _ratio_component(max_ratio),
            "dollar_score": _dollar_component(total_unusual_dollar_volume),
            "concentration_score": _concentration_component(dominant_pct),
            "otm_score": _otm_component(otm_call_vol, total_call_vol),
        },
        "top_contracts": top_contracts,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# UOA scoring — 0–100 scale
# ---------------------------------------------------------------------------

def _ratio_component(max_ratio: float) -> int:
    """Volume/OI ratio magnitude (0–40).

    Higher ratio = more aggressive new positioning.
    """
    if max_ratio >= 50:
        return 40    # extreme — someone is loading up
    if max_ratio >= 20:
        return 35
    if max_ratio >= 10:
        return 28
    if max_ratio >= 5:
        return 20
    if max_ratio >= 3:
        return 12
    return 5


def _dollar_component(dollar_vol: float) -> int:
    """Dollar volume of unusual contracts (0–30).

    Large notional = institutional, not retail noise.
    """
    if dollar_vol >= 5_000_000:
        return 30    # millions in premium = serious money
    if dollar_vol >= 1_000_000:
        return 25
    if dollar_vol >= 500_000:
        return 20
    if dollar_vol >= 100_000:
        return 14
    if dollar_vol >= 25_000:
        return 8
    return 3


def _concentration_component(dominant_pct: float) -> int:
    """Expiry concentration (0–15).

    All volume in one expiry = targeted bet on a catalyst.
    """
    if dominant_pct >= 90:
        return 15
    if dominant_pct >= 70:
        return 12
    if dominant_pct >= 50:
        return 8
    return 4


def _otm_component(otm_call_vol: int, total_call_vol: int) -> int:
    """OTM call skew (0–15).

    Volume concentrated in OTM calls = speculative directional bet.
    """
    if total_call_vol == 0:
        return 0
    otm_pct = otm_call_vol / total_call_vol * 100
    if otm_pct >= 80:
        return 15    # heavily OTM = aggressive bullish bet
    if otm_pct >= 60:
        return 12
    if otm_pct >= 40:
        return 8
    return 4


def _uoa_score(
    max_ratio: float,
    total_dollar_volume: float,
    dominant_pct: float,
    otm_call_vol: int,
    total_call_vol: int,
    num_contracts: int,
) -> int:
    """Compute composite UOA score (0–100)."""
    score = (
        _ratio_component(max_ratio)
        + _dollar_component(total_dollar_volume)
        + _concentration_component(dominant_pct)
        + _otm_component(otm_call_vol, total_call_vol)
    )
    return min(score, 100)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    universe_path = sys.argv[1] if len(sys.argv) > 1 else "universe.jsonl"
    watchlist_path = sys.argv[2] if len(sys.argv) > 2 else "watchlist.txt"
    out_path = sys.argv[3] if len(sys.argv) > 3 else "uoa.jsonl"

    universe_tickers = _load_universe_tickers(universe_path)
    watchlist_tickers = _load_watchlist(watchlist_path)
    all_tickers = sorted(universe_tickers | watchlist_tickers)

    if not all_tickers:
        print("no tickers to scan", file=sys.stderr)
        return 1

    print(
        f"scanning {len(all_tickers)} tickers for UOA "
        f"({len(universe_tickers)} from universe, {len(watchlist_tickers)} from watchlist)...",
        file=sys.stderr,
    )

    results: list[dict] = []
    for ticker in all_tickers:
        print(f"  scanning: {ticker}", file=sys.stderr)
        record = _scan_chain(ticker)
        if record:
            # Tag where this ticker came from
            sources: list[str] = []
            if ticker in universe_tickers:
                sources.append("universe")
            if ticker in watchlist_tickers:
                sources.append("watchlist")
            record["found_via"] = sources
            results.append(record)

    # Sort by UOA score descending
    results.sort(key=lambda r: r["uoa_score"], reverse=True)

    with open(out_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(
        f"found unusual activity in {len(results)}/{len(all_tickers)} tickers -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
