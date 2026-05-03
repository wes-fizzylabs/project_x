from __future__ import annotations

from pydantic import BaseModel, Field


class UniverseRecord(BaseModel, extra="allow"):
    """A single record from the market-intel pipeline universe.json.

    Core fields are typed; additional pipeline fields are preserved
    via extra="allow" so the model stays forward-compatible.
    """

    ticker: str
    company: str
    sector: str | None = None
    industry: str | None = None
    sources: list[str] = Field(default_factory=list)
    price_usd: float | None = None
    composite_score: float | None = None
    composite_label: str | None = None
    urgency_score: float | None = None
    urgency_label: str | None = None
    institutional_label: str | None = None
    action: str | None = None
    role: str | None = None
    value_usd: float | None = None
    insider: str | None = None
    trade_date: str | None = None
    filing_lag_days: int | None = None


class AnalystInput(BaseModel):
    """What the Orchestrator sends to the Analyst."""

    universe: list[UniverseRecord] = Field(description="Scored records from the market-intel pipeline")
    focus_areas: list[str] = Field(
        default_factory=list,
        description="Optional areas to emphasize (e.g., 'insider clusters', 'squeeze setups')",
    )


class AnalystOutput(BaseModel):
    """What the Analyst returns to the Orchestrator."""

    input: AnalystInput = Field(description="Echo of what was requested")
    content: str = Field(description="Final analysis in markdown")
    tickers_analyzed: int = Field(default=0)
    error: str | None = Field(default=None)

    @property
    def ok(self) -> bool:
        return self.error is None
