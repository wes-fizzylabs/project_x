"""Enrich shaped trades with options liquidity and short interest from Yahoo Finance.

Reads trades_shaped.jsonl, checks options chain availability, liquidity,
and short interest for each ticker, and writes trades_enriched.jsonl.

Usage (standalone):
    python -m market_intel.pipeline.yahoo                              # defaults
    python -m market_intel.pipeline.yahoo shaped.jsonl enriched.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

import yfinance as yf


# Target windows for long call strategy (months -> label)
_WINDOWS = {1: "1m", 3: "3m", 6: "6m"}


def _bucket_expirations(
    expirations: tuple[str, ...],
    today: date,
) -> dict[str, str | None]:
    """Find the nearest expiration to each target window.

    Returns e.g. {"1m": "2026-05-16", "3m": "2026-07-17", "6m": "2026-10-16"}
    or None for windows with no suitable expiration.
    """
    targets = {
        label: today + timedelta(days=months * 30)
        for months, label in _WINDOWS.items()
    }

    exp_dates = sorted(date.fromisoformat(e) for e in expirations)
    bucketed: dict[str, str | None] = {}

    for label, target in targets.items():
        best = None
        best_dist = float("inf")
        for ed in exp_dates:
            # Only consider expirations at or after today
            if ed < today:
                continue
            dist = abs((ed - target).days)
            if dist < best_dist:
                best_dist = dist
                best = ed
        bucketed[label] = best.isoformat() if best else None

    return bucketed


def _score_liquidity(calls_df, price_usd: float | None) -> dict:
    """Score options liquidity for the best ATM calls.

    Returns a summary dict with spread, volume, and open interest.
    Picks the highest-OI strike near the money for a more representative
    liquidity read than just the mathematically closest strike.
    """
    if calls_df is None or calls_df.empty:
        return {"tradeable": False}

    calls_df = calls_df.copy()

    # If no price provided, estimate from the options chain midpoint
    if not price_usd:
        # Use the strike where bid/ask are most balanced as a proxy
        has_quotes = calls_df[(calls_df["bid"] > 0) & (calls_df["ask"] > 0)]
        if has_quotes.empty:
            return {"tradeable": False}
        mid_idx = len(has_quotes) // 2
        price_usd = float(has_quotes.iloc[mid_idx]["strike"])

    # Find near-the-money strikes (within 5% of price)
    calls_df["dist_pct"] = ((calls_df["strike"] - price_usd) / price_usd).abs()
    near_money = calls_df[calls_df["dist_pct"] <= 0.05]

    # Fall back to closest strike if nothing within 5%
    if near_money.empty:
        near_money = calls_df.nsmallest(3, "dist_pct")

    # Pick the strike with the highest open interest among near-money
    oi_col = near_money["openInterest"].fillna(0)
    atm = near_money.loc[oi_col.idxmax()]

    bid = float(atm.get("bid", 0) or 0)
    ask = float(atm.get("ask", 0) or 0)
    mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
    spread_pct = ((ask - bid) / mid * 100) if mid > 0 else None
    volume = int(atm.get("volume", 0) or 0)
    open_interest = int(atm.get("openInterest", 0) or 0)
    iv = float(atm.get("impliedVolatility", 0) or 0)

    # Tradeable = reasonable spread and some open interest
    tradeable = (spread_pct is not None and spread_pct < 30 and open_interest >= 10)

    return {
        "tradeable": tradeable,
        "atm_strike": float(atm["strike"]),
        "bid": bid,
        "ask": ask,
        "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
        "volume": volume,
        "open_interest": open_interest,
        "implied_volatility": round(iv * 100, 1),  # as percentage
    }


def _options_multiplier(options: dict, price_usd: float | None = None) -> float:
    """Compute a 0.0–1.0 multiplier reflecting how executable this trade is.

    Starts at 1.0 and penalises for poor spread, low open interest,
    and high implied volatility (expensive premiums).

    No options chain doesn't zero the trade out — lower-priced stocks
    are still viable as direct share buys.
    """
    if not options.get("has_options"):
        # No options chain — still tradeable as shares.
        # Lower-priced stocks are better candidates for direct buys.
        p = price_usd or 250
        if p <= 20:
            return 0.6   # cheap enough to buy shares in bulk
        if p <= 50:
            return 0.5
        if p <= 100:
            return 0.4
        return 0.3        # expensive stock with no options — weakest setup

    liq = options.get("liquidity", {})
    if not liq or not liq.get("open_interest"):
        return 0.15  # chain exists but effectively dead

    mult = 1.0

    # Spread penalty — wider spread = harder to get a fair fill
    spread = liq.get("spread_pct")
    if spread is None or spread > 50:
        mult *= 0.3
    elif spread > 20:
        mult *= 0.6
    elif spread > 10:
        mult *= 0.85

    # Open interest penalty — thin OI = hard to enter/exit
    oi = liq.get("open_interest", 0)
    if oi < 10:
        mult *= 0.3
    elif oi < 100:
        mult *= 0.7
    elif oi < 500:
        mult *= 0.9

    # IV penalty — high IV = expensive premiums eating into edge
    iv = liq.get("implied_volatility", 0)
    if iv > 150:
        mult *= 0.6
    elif iv > 100:
        mult *= 0.75
    elif iv > 75:
        mult *= 0.9

    return round(mult, 2)


def _short_squeeze_multiplier(short_interest: dict) -> float:
    """Compute a multiplier for short squeeze potential.

    Unlike other multipliers, this can go ABOVE 1.0 — high short interest
    combined with insider buying actively boosts the signal because
    a squeeze amplifies the upside.
    """
    si_pct = short_interest.get("short_pct_float")
    if si_pct is None:
        return 1.0  # no data — neutral

    if si_pct >= 25:
        return 1.3   # extreme squeeze potential
    if si_pct >= 15:
        return 1.2   # significant squeeze setup
    if si_pct >= 5:
        return 1.1   # some squeeze potential
    return 1.0        # low SI — neutral, no boost


def _get_info(t: yf.Ticker) -> dict:
    """Safely fetch the ticker info dict (cached by yfinance internally)."""
    try:
        return t.info or {}
    except Exception:
        return {}


def collect_sector(info: dict) -> dict:
    """Extract sector and industry from a yfinance info dict."""
    sector = info.get("sector")
    industry = info.get("industry")
    if not sector and not industry:
        return {"available": False}
    return {
        "available": True,
        "sector": sector,
        "industry": industry,
    }


def collect_short_interest(info: dict) -> dict:
    """Extract short interest data from a yfinance info dict."""
    si_float = info.get("shortPercentOfFloat")
    si_shares = info.get("sharesShort")
    short_ratio = info.get("shortRatio")  # days to cover
    float_shares = info.get("floatShares")

    if si_float is None and si_shares is None:
        return {"available": False}

    return {
        "available": True,
        "float_shares": float_shares,
        "short_pct_float": round(si_float * 100, 1) if si_float else None,
        "shares_short": si_shares,
        "short_ratio": short_ratio,  # days to cover
    }


def collect_earnings(t: yf.Ticker, trade_date_str: str) -> dict:
    """Extract next earnings date and compute window alignment.

    Returns informational data — not used as a multiplier.
    """
    try:
        cal = t.calendar
    except Exception:
        cal = None

    # yfinance returns calendar as a dict or DataFrame depending on version
    earnings_date = None
    if cal is not None:
        if isinstance(cal, dict):
            # Newer yfinance: dict with 'Earnings Date' key (list of dates)
            ed = cal.get("Earnings Date") or cal.get("earningsDate")
            if isinstance(ed, list) and ed:
                earnings_date = ed[0]
            elif ed:
                earnings_date = ed
        else:
            # Older yfinance: DataFrame — try to pull Earnings Date row
            try:
                earnings_date = cal.loc["Earnings Date"][0]
            except Exception:
                pass

    if earnings_date is None:
        return {"available": False}

    # Normalise to a date string
    try:
        if hasattr(earnings_date, "date"):
            ed_date = earnings_date.date()
        elif isinstance(earnings_date, date):
            ed_date = earnings_date
        else:
            ed_date = date.fromisoformat(str(earnings_date)[:10])
    except Exception:
        return {"available": False}

    trade_d = date.fromisoformat(trade_date_str)
    days_away = (ed_date - trade_d).days

    # Which option window captures this earnings event?
    window_alignment = None
    for months, label in sorted(_WINDOWS.items()):
        window_end = trade_d + timedelta(days=months * 30)
        if ed_date <= window_end:
            window_alignment = label
            break

    # Timing note for the report
    if days_away < 0:
        timing_note = "earnings already passed"
    elif days_away <= 14:
        timing_note = "earnings imminent — likely in blackout window"
    elif days_away <= 30:
        timing_note = "insider bought before blackout window"
    elif days_away <= 90:
        timing_note = "earnings within quarter — mid-range catalyst"
    else:
        timing_note = "earnings distant — longer thesis"

    return {
        "available": True,
        "next_date": ed_date.isoformat(),
        "days_away": days_away,
        "window_alignment": window_alignment,
        "timing_note": timing_note,
    }


def collect_ticker_data(ticker: str, price_usd: float | None, trade_date: str | None = None) -> dict:
    """Gather options, short interest, and earnings data for a single ticker."""
    try:
        t = yf.Ticker(ticker)
    except Exception:
        return {
            "options": {"has_options": False},
            "short_interest": {"available": False},
            "sector": {"available": False},
            "earnings": {"available": False},
        }

    # --- options ---
    options: dict
    try:
        expirations = t.options
        if not expirations:
            options = {"has_options": False}
        else:
            today = date.today()
            bucketed = _bucket_expirations(expirations, today)

            # Score liquidity per target bucket instead of just the nearest expiry
            bucket_liquidity: dict[str, dict] = {}
            best_liq: dict | None = None
            for label in ("1m", "3m", "6m"):
                exp = bucketed.get(label)
                if not exp:
                    continue
                try:
                    chain = t.option_chain(exp)
                    liq = _score_liquidity(chain.calls, price_usd)
                    bucket_liquidity[label] = liq
                    # Track the best (most tradeable) bucket
                    if liq.get("tradeable") and (
                        best_liq is None or not best_liq.get("tradeable")
                    ):
                        best_liq = liq
                    elif best_liq is None:
                        best_liq = liq
                except Exception:
                    bucket_liquidity[label] = {"tradeable": False}

            options = {
                "has_options": True,
                "expirations_available": len(expirations),
                "nearest_expiry": expirations[0],
                "target_expiries": bucketed,
                "liquidity": best_liq or {"tradeable": False},
                "bucket_liquidity": bucket_liquidity,
            }
    except Exception:
        options = {"has_options": False}

    # --- info (single fetch, used for short interest + sector) ---
    info = _get_info(t)

    # --- short interest ---
    short_interest = collect_short_interest(info)

    # --- sector/industry ---
    sector = collect_sector(info)

    # --- earnings ---
    if trade_date:
        earnings = collect_earnings(t, trade_date)
    else:
        earnings = {"available": False}

    # --- price (already in info, no extra API call) ---
    price = info.get("currentPrice") or info.get("regularMarketPrice")

    return {
        "options": options,
        "short_interest": short_interest,
        "sector": sector,
        "earnings": earnings,
        "price": price,
    }


def main() -> int:
    in_path = sys.argv[1] if len(sys.argv) > 1 else "trades_shaped.jsonl"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "trades_enriched.jsonl"

    trades: list[dict] = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            trades.append(json.loads(line))

    seen: dict[str, dict] = {}  # cache per ticker
    for trade in trades:
        ticker = trade["ticker"]
        if ticker not in seen:
            print(f"  fetching: {ticker}", file=sys.stderr)
            seen[ticker] = collect_ticker_data(
                ticker, trade.get("price_usd"), trade.get("trade_date"),
            )
        data = seen[ticker]
        trade["options"] = data["options"]
        trade["short_interest"] = data["short_interest"]
        trade["earnings"] = data["earnings"]

        # Flatten sector/industry to top-level fields
        sec = data["sector"]
        if sec.get("available"):
            trade["sector"] = sec["sector"]
            trade["industry"] = sec["industry"]

        # --- composite scoring ---
        opt_mult = _options_multiplier(trade["options"], trade.get("price_usd"))
        si_mult = _short_squeeze_multiplier(trade["short_interest"])
        urgency = trade.get("urgency_score", 0)

        composite = urgency * opt_mult * si_mult
        trade["composite_score"] = round(composite, 1)
        trade["score_breakdown"] = {
            "urgency": urgency,
            "multipliers": {
                "options": opt_mult,
                "short_squeeze": si_mult,
            },
        }

    # Final ordering by composite score; tie-break on raw urgency
    trades.sort(
        key=lambda t: (t["composite_score"], t.get("urgency_score", 0)),
        reverse=True,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    no_opts = sum(1 for t in trades if not t["options"]["has_options"])
    print(
        f"enriched {len(trades)} trades ({no_opts} without options) -> {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
