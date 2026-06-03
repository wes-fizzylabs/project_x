from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from core.models.researcher import (
    NewsItem,
    PeerSnapshot,
    ResearcherInput,
    ResearcherOutput,
    TickerResearch,
)
from core.prompts import RESEARCHER_SYSTEM_PROMPT

from market_intel.pipeline.yahoo import collect_ticker_data
from market_intel.pipeline.sentiment import fetch_sentiment

_OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Only send fields the researcher needs — full records have options chains,
# score breakdowns, urgency signals, etc. that bloat past the 30K token rate limit.
_RESEARCHER_FIELDS = {
    "ticker", "company", "sector", "industry", "composite_score",
    "composite_label", "price_usd", "sources", "insider", "action",
    "role", "value_usd", "trade_date", "filing_lag_days",
}


def _build_user_message(input: ResearcherInput) -> tuple[str, list[dict], list[str]]:
    """Build the user message with universe records for the researcher."""
    # Filter to tickers above score threshold
    researched = []
    skipped = []
    for rec in input.universe:
        score = rec.get("composite_score") or 0
        if score >= input.score_threshold:
            researched.append(rec)
        else:
            skipped.append(rec.get("ticker", "???"))

    # Sort by composite score descending and cap at max_tickers
    researched.sort(key=lambda r: r.get("composite_score", 0), reverse=True)
    if len(researched) > input.max_tickers:
        overflow = researched[input.max_tickers:]
        researched = researched[:input.max_tickers]
        skipped.extend(r.get("ticker", "???") for r in overflow)

    parts = ["<universe_data>"]
    for rec in researched:
        ticker = rec.get("ticker", "???")
        score = rec.get("composite_score", 0)
        slim = {k: v for k, v in rec.items() if k in _RESEARCHER_FIELDS}
        parts.append(f'  <record ticker="{ticker}" composite_score="{score}">')
        parts.append(f"    {json.dumps(slim)}")
        parts.append("  </record>")
    parts.append("</universe_data>")

    parts.append(
        f"\nResearch the {len(researched)} tickers above."
        f" {len(skipped)} tickers were below the {input.score_threshold} score threshold and skipped."
    )
    if skipped:
        parts.append(f"Skipped: {', '.join(skipped)}")
    if input.focus_areas:
        parts.append(f"Focus areas: {', '.join(input.focus_areas)}")

    return "\n".join(parts), researched, skipped


def _enrich_peer(ticker: str) -> PeerSnapshot | None:
    """Call pipeline functions directly to get snapshot + sentiment for a peer."""
    try:
        from datetime import date

        snapshot = collect_ticker_data(ticker.upper(), price_usd=None, trade_date=date.today().isoformat())
        sentiment = fetch_sentiment(ticker.upper())

        return PeerSnapshot(
            ticker=ticker.upper(),
            name=snapshot.get("company", ticker.upper()),
            price=snapshot.get("price"),
            market_cap=snapshot.get("market_cap"),
            short_interest_pct=snapshot.get("short_interest", {}).get("short_pct_float"),
            sentiment_bull_pct=sentiment.get("bullish_pct"),
            sentiment_volume=sentiment.get("recent_messages"),
            why_relevant="",  # filled from LLM output
            notable=None,
        )
    except Exception:
        return None



def _extract_research_json(text: str) -> list[dict]:
    """Extract the JSON array from <research_output> tags or raw JSON."""
    # Try tagged output first — handle optional markdown code fences (```json ... ```)
    match = re.search(
        r"<research_output>\s*(?:```json\s*)?(\[.*\])\s*(?:```)?\s*</research_output>",
        text, re.DOTALL,
    )
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Fallback: bracket matching — find the LARGEST valid JSON array (not a nested one).
    # This handles truncated output where the outer array never closes: we parse
    # each complete [...] and return whichever contains TickerResearch-shaped dicts.
    start = text.find("[")
    best: list[dict] = []
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                            # Prefer arrays of TickerResearch (have "ticker" + "news" keys)
                            if "ticker" in parsed[0] and "news" in parsed[0]:
                                return parsed
                            # Track largest non-research array as fallback
                            if len(parsed) > len(best):
                                best = parsed
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("[", start + 1)

    return best


def _parse_research_output(raw_items: list[dict], peer_data: dict[str, PeerSnapshot]) -> list[TickerResearch]:
    """Parse LLM JSON output into TickerResearch models, merging peer MCP data."""
    results = []
    for item in raw_items:
        news = []
        for n in item.get("news", []):
            news.append(NewsItem(
                headline=n.get("headline", ""),
                source=n.get("source", "unknown"),
                url=n.get("url"),
                published=n.get("published"),
                relevance=n.get("relevance", ""),
            ))

        # Merge LLM peer context with MCP snapshot data
        peers = []
        for p in item.get("peers", []):
            pticker = p.get("ticker", "").upper()
            if pticker in peer_data:
                snapshot = peer_data[pticker]
                peers.append(PeerSnapshot(
                    ticker=pticker,
                    name=p.get("name", snapshot.name),
                    price=snapshot.price,
                    market_cap=snapshot.market_cap,
                    short_interest_pct=snapshot.short_interest_pct,
                    sentiment_bull_pct=snapshot.sentiment_bull_pct,
                    sentiment_volume=snapshot.sentiment_volume,
                    why_relevant=p.get("why_relevant", ""),
                    notable=p.get("notable"),
                ))
            else:
                peers.append(PeerSnapshot(
                    ticker=pticker,
                    name=p.get("name", pticker),
                    price=p.get("price"),
                    market_cap=p.get("market_cap"),
                    short_interest_pct=p.get("short_interest_pct"),
                    sentiment_bull_pct=p.get("sentiment_bull_pct"),
                    sentiment_volume=p.get("sentiment_volume"),
                    why_relevant=p.get("why_relevant", ""),
                    notable=p.get("notable"),
                ))

        results.append(TickerResearch(
            ticker=item.get("ticker", ""),
            composite_score=item.get("composite_score", 0),
            news=news,
            sector_context=item.get("sector_context", ""),
            peers=peers,
            catalyst_summary=item.get("catalyst_summary", ""),
            risk_flags=item.get("risk_flags", []),
        ))

    return results


def run(input: ResearcherInput) -> ResearcherOutput:
    """Run the researcher agent: web search for news + peer enrichment."""
    client = anthropic.Anthropic(max_retries=5)

    user_message, researched_records, skipped = _build_user_message(input)

    if not researched_records:
        return ResearcherOutput(
            researched=[],
            skipped_tickers=skipped,
            run_timestamp=datetime.now(timezone.utc),
            error="No tickers above score threshold",
        )

    # Format the prompt with max_peers
    system_prompt = RESEARCHER_SYSTEM_PROMPT.replace("{max_peers}", str(input.max_peers))

    # Step 1: LLM with web search — news discovery + peer identification
    # Use streaming to avoid 10-minute timeout on long web-search requests
    print(f"[researcher] Researching {len(researched_records)} tickers (web search + peer discovery)")
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=32000,
        system=system_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        response = stream.get_final_message()

    web_searches = sum(
        1 for block in response.content
        if getattr(block, "type", None) == "web_search_tool_result"
    )
    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw_text = "\n".join(text_blocks)

    # Debug: save raw LLM text
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    raw_path = _OUTPUT_DIR / f"research_raw_{ts}.txt"
    raw_path.write_text(raw_text, encoding="utf-8")

    # Step 2: Parse LLM output to find peer tickers
    raw_items = _extract_research_json(raw_text)
    if not raw_items:
        print(f"[researcher] Warning: could not parse JSON from LLM output ({len(raw_text)} chars)")
        print(f"[researcher] Raw output saved to {raw_path}")

    # Collect unique peer tickers (exclude universe tickers)
    universe_tickers = {r.get("ticker", "").upper() for r in input.universe}
    peer_tickers: set[str] = set()
    for item in raw_items:
        for p in item.get("peers", []):
            pticker = p.get("ticker", "").upper()
            if pticker and pticker not in universe_tickers:
                peer_tickers.add(pticker)

    # Step 3: Enrich peers via direct pipeline function calls
    print(f"[researcher] Enriching {len(peer_tickers)} peer companies")
    peer_data: dict[str, PeerSnapshot] = {}
    mcp_calls = 0
    for pticker in sorted(peer_tickers):
        print(f"  enriching peer: {pticker}")
        snapshot = _enrich_peer(pticker)
        if snapshot:
            peer_data[pticker] = snapshot
            mcp_calls += 2  # market_snapshot + get_sentiment

    # Step 4: Merge and build final output
    ticker_research = _parse_research_output(raw_items, peer_data)

    # Save parsed output for debugging
    debug_path = _OUTPUT_DIR / f"research_{ts}.json"
    debug_path.write_text(json.dumps(raw_items, indent=2, default=str), encoding="utf-8")

    print(f"[researcher] Done — {len(ticker_research)} tickers researched, {web_searches} web searches, {mcp_calls} peer enrichments")

    return ResearcherOutput(
        researched=ticker_research,
        skipped_tickers=skipped,
        run_timestamp=datetime.now(timezone.utc),
        total_web_searches=web_searches,
        total_mcp_calls=mcp_calls,
    )
