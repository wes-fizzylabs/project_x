from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class NewsItem(BaseModel):
    """A single news/catalyst finding from web search."""

    headline: str
    source: str  # e.g. "Reuters", "SEC filing", "PR Newswire"
    url: str | None = None
    published: datetime | None = None
    relevance: str  # one-liner on why this matters to the thesis


class PeerSnapshot(BaseModel):
    """A sector peer discovered by the researcher, enriched via MCP tools."""

    ticker: str
    name: str
    price: float | None = None
    market_cap: float | None = None
    short_interest_pct: float | None = None
    sentiment_bull_pct: float | None = None
    sentiment_volume: int | None = None
    why_relevant: str  # "direct competitor in building products, similar cap"
    notable: str | None = None  # anything that stood out


class TickerResearch(BaseModel):
    """Research output for a single universe ticker."""

    ticker: str
    composite_score: float
    news: list[NewsItem] = Field(default_factory=list)
    sector_context: str
    peers: list[PeerSnapshot] = Field(default_factory=list, description="Max 5 peers")
    catalyst_summary: str  # plain-english: what could move this and when
    risk_flags: list[str] = Field(default_factory=list)


class ResearcherInput(BaseModel):
    """What the Orchestrator sends to the Researcher."""

    universe: list[dict] = Field(description="Top-scored records from universe.json")
    focus_areas: list[str] = Field(default_factory=list)
    score_threshold: float = Field(
        default=30.0,
        description="Minimum composite score to research (tickers below are skipped)",
    )
    max_tickers: int = Field(
        default=5,
        description="Max tickers to research (top N by composite score after threshold filter)",
    )
    max_peers: int = Field(default=5, description="Max peer companies per ticker")


class ResearcherOutput(BaseModel):
    """What the Researcher returns to the Orchestrator."""

    researched: list[TickerResearch] = Field(default_factory=list)
    skipped_tickers: list[str] = Field(default_factory=list)
    run_timestamp: datetime
    total_web_searches: int = 0
    total_mcp_calls: int = 0
    error: str | None = Field(default=None)

    @property
    def ok(self) -> bool:
        return self.error is None
