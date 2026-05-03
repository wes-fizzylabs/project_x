"""Generate human-readable interpretation labels for scored universe records.

Pure functions — no I/O, no side effects. Each function takes a record dict
and returns a label string. Called as a final pass in enrich_universe.py.

Every label maps directly to the scoring thresholds defined in the source
modules (models.py, collect_yahoo_finance.py, st_sentiment.py, merge_sources.py,
scan_uoa.py, scrape_13f.py). See experiments/label-spec.md for the full mapping.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_dollars(v: float) -> str:
    """$64591772 -> '$64.6M', $595308 -> '$595K'"""
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


# ---------------------------------------------------------------------------
# 1. Urgency
# ---------------------------------------------------------------------------

_URGENCY_TIERS = [(70, "very high"), (55, "high"), (40, "moderate"), (0, "low")]

_FILING_LAG_DESC = {23: "same-day filing", 18: "next-day filing", 9: "2-day filing", 4: "delayed filing"}
_ROLE_DESC = {23: "C-suite executive", 14: "board director", 9: "10%+ holder", 4: "other insider"}
_SIZE_DESC = {23: "$1M+ purchase", 18: "$500K+ purchase", 14: "$100K+ purchase", 9: "$50K+ purchase", 4: "small purchase"}
_CLUSTER_DESC = {24: "3+ insiders buying", 14: "2 insiders buying"}
_PRICE_DESC = {7: "under $20 stock", 5: "under $50 stock", 3: "under $100 stock", 1: "$100+ stock"}

_SIGNAL_MAPS = {
    "filing_lag": _FILING_LAG_DESC,
    "role": _ROLE_DESC,
    "size": _SIZE_DESC,
    "cluster": _CLUSTER_DESC,
    "price": _PRICE_DESC,
}

_FLAG_PENALTY_DESC = {
    0.4: "option exercise (not open-market)",
    0.7: "amended filing",
    0.28: "amended option exercise",
}


def urgency_label(record: dict) -> str | None:
    score = record.get("urgency_score")
    if score is None:
        return None

    tier = next(t for thresh, t in _URGENCY_TIERS if score >= thresh)

    signals = record.get("urgency_signals") or {}
    # Build (points, description) pairs for non-zero signals
    parts: list[tuple[int, str]] = []
    for key, desc_map in _SIGNAL_MAPS.items():
        pts = signals.get(key, 0)
        if pts > 0 and pts in desc_map:
            parts.append((pts, desc_map[pts]))

    # Top 3 contributors by point value
    parts.sort(key=lambda x: x[0], reverse=True)
    drivers = ", ".join(desc for _, desc in parts[:3])

    # Flag quality penalty
    flag_q = signals.get("flag_quality", 1.0)
    if flag_q < 1.0:
        flag_desc = _FLAG_PENALTY_DESC.get(flag_q)
        if not flag_desc:
            flag_desc = f"flag penalty (x{flag_q})"
        penalty = f" [penalized: {flag_desc}]"
    else:
        penalty = ""

    base = f"{tier} — {drivers}" if drivers else tier
    return f"{base}{penalty}"


# ---------------------------------------------------------------------------
# 2. Composite
# ---------------------------------------------------------------------------

_COMPOSITE_TIERS = [(60, "strong"), (35, "moderate"), (15, "weak"), (0, "very weak")]


def composite_label(record: dict) -> str | None:
    score = record.get("composite_score")
    if score is None:
        return None

    tier = next(t for thresh, t in _COMPOSITE_TIERS if score >= thresh)

    breakdown = record.get("score_breakdown") or {}
    mults = breakdown.get("multipliers") or {}

    # Determine base source
    urgency = breakdown.get("urgency")
    base = breakdown.get("base")
    base_source = breakdown.get("base_source")

    if urgency is not None:
        base_desc = f"urgency {urgency}"
    elif base is not None and base_source:
        source_name = "UOA" if base_source == "uoa_score" else "discovery"
        base_desc = f"{source_name} base {base}"
    else:
        base_desc = ""

    # Note multipliers that notably help or hurt
    effects: list[str] = []
    for name, val in mults.items():
        if name in ("trending_confirmation", "uoa_confirmation"):
            effects.append(f"confirmation boost ({name.split('_')[0]} x{val})")
        elif val < 0.7:
            effects.append(f"penalized by {_mult_name(name)} (x{val})")
        elif val > 1.05:
            effects.append(f"boosted by {_mult_name(name)} (x{val})")

    if base_desc and effects:
        return f"{tier} — {base_desc}, {', '.join(effects)}"
    if base_desc:
        return f"{tier} — {base_desc}"
    return tier


def _mult_name(key: str) -> str:
    return {
        "options": "options liquidity",
        "short_squeeze": "short interest",
        "sentiment": "sentiment/crowd",
    }.get(key, key)


# ---------------------------------------------------------------------------
# 3. Options multiplier
# ---------------------------------------------------------------------------

def options_label(record: dict) -> str | None:
    opts = record.get("options")
    if not opts:
        return None

    breakdown = record.get("score_breakdown") or {}
    mult = (breakdown.get("multipliers") or {}).get("options")
    if mult is None:
        return None

    if not opts.get("has_options"):
        price = record.get("price_usd") or 0
        if price <= 20:
            return "no options — shares only (low price)"
        if price <= 50:
            return "no options — shares only"
        if price <= 100:
            return "no options — shares only (expensive)"
        return "no options — shares only (very expensive)"

    liq = opts.get("liquidity") or {}
    oi = liq.get("open_interest", 0) or 0

    if not liq or not oi:
        return "options exist but illiquid"

    if mult >= 0.85:
        return "liquid options"

    # Identify worst penalty reasons
    reasons: list[str] = []
    spread = liq.get("spread_pct")
    if spread is not None:
        if spread > 50:
            reasons.append("very wide spreads")
        elif spread > 20:
            reasons.append("wide spreads")
        elif spread > 10:
            reasons.append("moderate spreads")

    if oi < 10:
        reasons.append("very thin OI")
    elif oi < 100:
        reasons.append("thin OI")
    elif oi < 500:
        reasons.append("moderate OI")

    iv = liq.get("implied_volatility", 0) or 0
    if iv > 150:
        reasons.append("very high IV")
    elif iv > 100:
        reasons.append("high IV")
    elif iv > 75:
        reasons.append("elevated IV")

    detail = ", ".join(reasons)
    if mult < 0.5:
        return f"poor liquidity — {detail}" if detail else "poor liquidity"
    return f"options with liquidity issues — {detail}" if detail else "options with liquidity issues"


# ---------------------------------------------------------------------------
# 4. Short squeeze multiplier
# ---------------------------------------------------------------------------

def short_squeeze_label(record: dict) -> str | None:
    si = record.get("short_interest") or {}
    if not si.get("available"):
        return "no data — neutral"

    pct = si.get("short_pct_float")
    if pct is None:
        return "SI% unavailable — neutral"

    if pct >= 25:
        return f"extreme SI ({pct}%) — strong squeeze catalyst"
    if pct >= 15:
        return f"high SI ({pct}%) — squeeze potential"
    if pct >= 5:
        return f"moderate SI ({pct}%) — some squeeze potential"
    return f"low SI ({pct}%) — neutral"


# ---------------------------------------------------------------------------
# 5. Sentiment multiplier
# ---------------------------------------------------------------------------

_ATT_LABELS = [(100_000, "heavily watched"), (25_000, "well-known"), (5_000, "some following"), (0, "under the radar")]
_POL_LABELS = [(80, "bullish hype"), (65, "bullish consensus"), (40, "neutral sentiment"), (0, "bearish lean")]


def sentiment_label(record: dict) -> str | None:
    sent = record.get("sentiment") or {}
    if not sent.get("available"):
        return "no data — neutral (ideal)"

    watchlist = sent.get("watchlist_count", 0) or 0
    bull_pct = sent.get("bullish_pct", 50) or 50

    att = next(l for thresh, l in _ATT_LABELS if watchlist >= thresh)
    pol = next(l for thresh, l in _POL_LABELS if bull_pct >= thresh)

    # Add a takeaway
    breakdown = record.get("score_breakdown") or {}
    mult = (breakdown.get("multipliers") or {}).get("sentiment")
    if mult is not None and mult >= 0.95:
        note = "ideal entry"
    elif mult is not None and mult <= 0.65:
        note = "crowd already in"
    else:
        note = None

    base = f"{att}, {pol}"
    return f"{base} — {note}" if note else base


# ---------------------------------------------------------------------------
# 6. Discovery score
# ---------------------------------------------------------------------------

_DISC_TIERS = [(80, "very high"), (60, "high"), (40, "moderate"), (0, "low")]

_WL_DESC = {40: "very obscure", 35: "low profile", 25: "moderate following", 15: "well-known", 8: "popular", 3: "heavily watched"}
_VEL_DESC = {35: "extreme message spike", 28: "high message spike", 20: "elevated chatter", 12: "above-normal chatter", 5: "normal chatter", 0: None}
_BULL_DESC = {25: "strong bullish consensus", 18: "bullish leaning", 10: "mixed sentiment", 5: "bearish leaning", 0: None}


def discovery_label(record: dict) -> str | None:
    score = record.get("discovery_score")
    if score is None:
        return None

    tier = next(t for thresh, t in _DISC_TIERS if score >= thresh)

    signals = record.get("discovery_signals") or {}
    watchlist = signals.get("watchlist_count", 0) or 0
    msgs = signals.get("recent_messages", 0) or 0
    bull = signals.get("recent_bullish", 0) or 0

    # Reconstruct sub-scores to identify descriptions
    # (mirrors merge_sources.py:_discovery_score thresholds)
    parts: list[str] = []

    # Watchlist factor
    if watchlist < 2_000:
        parts.append("very obscure")
    elif watchlist < 5_000:
        parts.append("low profile")
    elif watchlist < 10_000:
        parts.append("moderate following")
    elif watchlist < 25_000:
        parts.append("well-known")

    # Velocity factor
    if watchlist > 0 and msgs > 0:
        velocity = msgs / (watchlist / 1000)
        if velocity >= 10:
            parts.append("extreme message spike")
        elif velocity >= 5:
            parts.append("high message spike")
        elif velocity >= 2:
            parts.append("elevated chatter")

    # Bullish factor
    if msgs > 0:
        bull_pct = bull / msgs * 100
        if bull_pct >= 80:
            parts.append("strong bullish consensus")
        elif bull_pct >= 60:
            parts.append("bullish leaning")

    detail = ", ".join(parts)
    return f"{tier} — {detail}" if detail else tier


# ---------------------------------------------------------------------------
# 7. UOA score
# ---------------------------------------------------------------------------

_UOA_TIERS = [(90, "extreme"), (70, "strong"), (50, "moderate"), (0, "mild")]


def uoa_label(record: dict) -> str | None:
    uoa = record.get("uoa")
    if not uoa or uoa.get("uoa_score") is None:
        return None

    score = uoa["uoa_score"]
    tier = next(t for thresh, t in _UOA_TIERS if score >= thresh)

    dollar_vol = uoa.get("total_dollar_volume") or 0
    cp_ratio = uoa.get("call_put_ratio")
    otm_pct = uoa.get("otm_call_pct", 0) or 0

    # Direction
    if cp_ratio is not None and cp_ratio > 2 and otm_pct > 70:
        direction = "bullish positioning"
    elif cp_ratio is not None and cp_ratio < 0.5:
        direction = "bearish positioning (put-heavy)"
    elif cp_ratio is not None:
        direction = f"balanced C/P ({cp_ratio})"
    else:
        direction = "calls only" if otm_pct > 0 else "direction unclear"

    vol_str = _fmt_dollars(dollar_vol) if dollar_vol else ""
    parts = [p for p in [f"{vol_str} volume" if vol_str else "", direction] if p]
    detail = ", ".join(parts)

    return f"{tier} — {detail}" if detail else tier


# ---------------------------------------------------------------------------
# 8. Institutional
# ---------------------------------------------------------------------------

_INST_TIERS = [
    (1000, "very widely held"),
    (200, "widely held"),
    (50, "moderately held"),
    (1, "lightly held"),
    (0, "no institutional holders found"),
]


def institutional_label(record: dict) -> str | None:
    inst = record.get("institutional")
    if not inst or not inst.get("available"):
        return None

    total = inst.get("total_holders", 0)
    tier = next(t for thresh, t in _INST_TIERS if total >= thresh)

    notable = inst.get("notable_funds") or []
    if notable:
        names = ", ".join(f["fund"] for f in notable[:3])
        return f"{tier} ({total} institutions) — notable: {names}"

    return f"{tier} ({total} institutions)"


# ---------------------------------------------------------------------------
# Master function
# ---------------------------------------------------------------------------

def add_labels(record: dict) -> None:
    """Add all interpretation labels to a record dict in-place."""
    label = urgency_label(record)
    if label:
        record["urgency_label"] = label

    label = composite_label(record)
    if label:
        record["composite_label"] = label

    # Multiplier labels go inside score_breakdown
    breakdown = record.get("score_breakdown") or {}
    labels: dict[str, str] = {}

    ol = options_label(record)
    if ol:
        labels["options"] = ol

    sl = short_squeeze_label(record)
    if sl:
        labels["short_squeeze"] = sl

    sl2 = sentiment_label(record)
    if sl2:
        labels["sentiment"] = sl2

    if labels:
        breakdown["multiplier_labels"] = labels
        record["score_breakdown"] = breakdown

    label = discovery_label(record)
    if label:
        record["discovery_label"] = label

    label = uoa_label(record)
    if label:
        record["uoa_label"] = label

    label = institutional_label(record)
    if label:
        record["institutional_label"] = label
