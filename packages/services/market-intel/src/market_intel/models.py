"""Pydantic model for a shaped OpenInsider trade.

The raw JSONL from scrape_openinsider.py contains display strings
("$10.33", "+200,000", "+16%", "Dir, 10%"). This module parses those
into typed fields and surfaces the signals a downstream analysis agent
reads first: insider role category, dollar size, ownership delta, and
filing lag.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, computed_field

# Role codes OpenInsider uses in the `insider_title` column.
_EXECUTIVE_CODES = {
    "CEO", "CFO", "COO", "CAO", "CTO", "CIO", "CMO", "CCO",
    "Pres", "President", "VP", "EVP", "SVP", "GC", "Sec", "Treas",
    "Officer",
}
_DIRECTOR_CODES = {"Dir", "Director"}
_MAJOR_HOLDER_CODES = {"10%"}


def _parse_money(s: str) -> Optional[float]:
    """'+$2,065,100' -> 2065100.0   '-$1,000' -> -1000.0   '' -> None"""
    if not s:
        return None
    cleaned = s.replace("$", "").replace(",", "").replace("+", "").strip()
    return float(cleaned) if cleaned else None


def _parse_int(s: str) -> Optional[int]:
    """'+200,000' -> 200000   '1,471,000' -> 1471000   '' -> None"""
    if not s:
        return None
    cleaned = s.replace(",", "").replace("+", "").strip()
    return int(cleaned) if cleaned else None


def _parse_pct(s: str) -> Optional[float]:
    """'+16%' -> 16.0   '-2.5%' -> -2.5   '' -> None"""
    if not s:
        return None
    cleaned = s.replace("%", "").replace("+", "").strip()
    # OpenInsider uses 'New' for brand-new positions (infinite % change).
    if cleaned.lower() == "new":
        return None
    return float(cleaned) if cleaned else None


def _split_roles(title: str) -> list[str]:
    """'Dir, 10%' -> ['Dir', '10%']"""
    return [t.strip() for t in re.split(r"[,/]", title) if t.strip()]


def _categorize_roles(roles: list[str]) -> str:
    """Highest-signal category wins: executive > director > major_holder > other."""
    role_set = set(roles)
    if role_set & _EXECUTIVE_CODES:
        return "executive"
    if role_set & _DIRECTOR_CODES:
        return "director"
    if role_set & _MAJOR_HOLDER_CODES:
        return "major_holder"
    return "other"


class InsiderTrade(BaseModel):
    """A single shaped row. Call `to_agent_payload()` for the agent-facing dict."""

    # --- identity ---
    ticker: str
    company_name: str

    # --- trade mechanics ---
    trade_type_code: str           # 'P', 'S', etc.
    trade_type_label: str          # 'Purchase', 'Sale'
    price_usd: Optional[float]
    qty: Optional[int]             # signed: +buy / -sell
    value_usd: Optional[float]     # signed
    owned_after: Optional[int]
    delta_own_pct: Optional[float] # signed; None if 'New' or empty

    # --- insider ---
    insider_name: str
    insider_roles: list[str]
    role_category: str             # executive | director | major_holder | other

    # --- timing ---
    trade_date: date
    filing_datetime: datetime
    filing_lag_days: int

    # --- co-filing dedup ---
    co_filers: list[str] = Field(default_factory=list)  # other filers of same transaction

    # --- flags & context ---
    # SEC Form 4 footnote codes from OpenInsider's first column.
    # Key codes that affect signal quality:
    #   M = exercise/conversion of derivative security (option exercise)
    #   A = amendment (corrected/restated filing)
    #   D = direct ownership form (neutral — just indicates holding type)
    flags_raw: str
    sec_form4_url: Optional[str] = None
    insider_detail: Optional[str] = None
    scraped_at: Optional[datetime] = None

    @computed_field
    @property
    def is_purchase(self) -> bool:
        return self.trade_type_code == "P"

    def flag_quality(self) -> float:
        """0.0–1.0 multiplier reflecting how the acquisition method affects signal strength.

        Open-market purchases (no flags or D-only) are the strongest signal.
        Option exercises (M) are much weaker — driven by expiration calendars,
        not conviction. Amendments (A) indicate corrected/restated filings
        where timing data may be unreliable.
        """
        flags = self.flags_raw or ""
        mult = 1.0
        if "M" in flags:
            mult *= 0.4  # exercise of derivative — not discretionary buying
        if "A" in flags:
            mult *= 0.7  # amended filing — stale or corrected data
        return round(mult, 2)

    # --- urgency scoring (per-trade components) ---

    def filing_lag_score(self) -> int:
        """0–23 pts. Faster filing = higher conviction signal."""
        if self.filing_lag_days <= 0:
            return 23
        if self.filing_lag_days == 1:
            return 18
        if self.filing_lag_days == 2:
            return 9
        return 4

    def role_score(self) -> int:
        """0–23 pts. Executive insiders have the deepest information edge."""
        _scores = {"executive": 23, "director": 14, "major_holder": 9, "other": 4}
        return _scores.get(self.role_category, 4)

    def size_score(self) -> int:
        """0–23 pts. Larger dollar commitment = stronger conviction."""
        v = abs(self.value_usd or 0)
        if v >= 1_000_000:
            return 23
        if v >= 500_000:
            return 18
        if v >= 100_000:
            return 14
        if v >= 50_000:
            return 9
        return 4

    def price_score(self) -> int:
        """0–7 pts. Lower-priced stocks offer more options upside per dollar."""
        p = self.price_usd or 0
        if p <= 20:
            return 7
        if p <= 50:
            return 5
        if p <= 100:
            return 3
        return 1

    @classmethod
    def from_raw(cls, row: dict) -> "InsiderTrade":
        trade_type = row.get("trade_type", "") or ""
        code, _, label = trade_type.partition(" - ")

        roles = _split_roles(row.get("insider_title", "") or "")

        trade_d = datetime.strptime(row["trade_date"], "%Y-%m-%d").date()
        filing_dt = datetime.strptime(row["filing_datetime"], "%Y-%m-%d %H:%M:%S")

        delta = _parse_pct(row.get("delta_own_pct", ""))
        # OpenInsider's ΔOwn% is unreliable for 10% owners / multi-entity
        # filers — e.g. a purchase showing -242%. Null out when direction
        # contradicts the trade action.
        is_buy = code.strip() == "P"
        if delta is not None:
            if (is_buy and delta < 0) or (not is_buy and delta > 0):
                delta = None

        return cls(
            ticker=row["ticker"],
            company_name=row["company_name"],
            trade_type_code=code.strip(),
            trade_type_label=label.strip(),
            price_usd=_parse_money(row.get("price", "")),
            qty=_parse_int(row.get("qty", "")),
            value_usd=_parse_money(row.get("value_usd", "")),
            owned_after=_parse_int(row.get("owned_after", "")),
            delta_own_pct=delta,
            insider_name=row["insider_name"],
            insider_roles=roles,
            role_category=_categorize_roles(roles),
            trade_date=trade_d,
            filing_datetime=filing_dt,
            filing_lag_days=(filing_dt.date() - trade_d).days,
            flags_raw=row.get("flags", "") or "",
            sec_form4_url=row.get("sec_form4_url"),
            insider_detail=row.get("insider_detail"),
            scraped_at=(
                datetime.fromisoformat(row["scraped_at"])
                if row.get("scraped_at") else None
            ),
        )

    def to_agent_payload(self) -> dict:
        """Compact dict ordered by what a trading-analysis agent reads first.

        Drops archival noise (scraped_at, full address blob) and groups fields
        into: identity, signal, size, insider, timing, context.
        """
        return {
            # identity
            "ticker": self.ticker,
            "company": self.company_name,
            # signal (at-a-glance)
            "action": self.trade_type_label,
            "role": self.role_category,
            "flags": self.flags_raw or None,
            # size
            "value_usd": self.value_usd,
            "price_usd": self.price_usd,
            "qty": self.qty,
            "delta_own_pct": self.delta_own_pct,
            "owned_after": self.owned_after,
            # insider
            "insider": self.insider_name,
            "insider_titles": self.insider_roles,
            **({"co_filers": self.co_filers} if self.co_filers else {}),
            # timing
            "trade_date": self.trade_date.isoformat(),
            "filing_datetime": self.filing_datetime.isoformat(),
            "filing_lag_days": self.filing_lag_days,
            # context
            "sec_form4_url": self.sec_form4_url,
        }
