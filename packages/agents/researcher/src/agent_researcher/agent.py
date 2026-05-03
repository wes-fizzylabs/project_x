import anthropic
from core.models import ResearcherInput, ResearcherOutput
from core.models.searcher import MarketSignal, SourceType
from core.prompts import RESEARCHER_SYSTEM_PROMPT


def _format_signal(signal: MarketSignal) -> str:
    """Format a single signal as XML for the prompt."""
    parts = [f'  <signal source="{signal.source_type.value}" ticker="{signal.ticker}">']
    parts.append(f"    <headline>{signal.headline}</headline>")

    if signal.body:
        parts.append(f"    <body>{signal.body}</body>")
    if signal.url:
        parts.append(f"    <url>{signal.url}</url>")
    if signal.published_at:
        parts.append(f"    <date>{signal.published_at.isoformat()}</date>")

    # Insider trade fields
    if signal.source_type == SourceType.INSIDER_TRADE:
        if signal.insider_name:
            parts.append(f"    <insider name=\"{signal.insider_name}\" role=\"{signal.insider_role}\" />")
        if signal.transaction_type:
            parts.append(f"    <transaction type=\"{signal.transaction_type}\" shares=\"{signal.shares}\" amount=\"${signal.dollar_amount:,.2f}\" />")

    # Filing fields
    if signal.filing_type:
        parts.append(f"    <filing type=\"{signal.filing_type}\" />")

    # Sentiment
    if signal.sentiment_score is not None:
        parts.append(f"    <sentiment score=\"{signal.sentiment_score}\" />")

    parts.append("  </signal>")
    return "\n".join(parts)


def _build_user_message(input: ResearcherInput) -> str:
    """Serialize structured signals into XML for the researcher prompt."""
    searcher = input.searcher_output

    # Collect all unique tickers from signals (covers both targeted and broad scan)
    all_tickers = list(dict.fromkeys(
        s.ticker for s in searcher.signals
    ))

    parts = ["<searcher_data>"]

    for ticker in all_tickers:
        signals = searcher.for_ticker(ticker)
        is_broad = ticker not in searcher.input.tickers
        label = f' source="broad_scan"' if is_broad else ""
        parts.append(f'  <ticker symbol="{ticker}" signal_count="{len(signals)}"{label}>')
        for signal in signals:
            parts.append(_format_signal(signal))
        parts.append("  </ticker>")

    if searcher.errors:
        parts.append("  <errors>")
        for err in searcher.errors:
            parts.append(f"    <error>{err}</error>")
        parts.append("  </errors>")

    parts.append("</searcher_data>")

    parts.append(f"\nResearch the following tickers: {', '.join(all_tickers)}")

    if input.focus_areas:
        parts.append(f"Focus areas: {', '.join(input.focus_areas)}")

    return "\n".join(parts)


def _count_searches(response) -> int:
    """Count how many web searches the model performed via tool use."""
    count = 0
    for block in response.content:
        if block.type == "tool_use" and block.name == "web_search":
            count += 1
    return count


def run(input: ResearcherInput) -> ResearcherOutput:
    """Run the researcher agent with agentic web search."""
    client = anthropic.Anthropic()

    user_message = _build_user_message(input)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=RESEARCHER_SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305"}],
        messages=[{"role": "user", "content": user_message}],
    )

    # Extract the final text content from the response
    text_blocks = [block.text for block in response.content if block.type == "text"]
    content = "\n".join(text_blocks)

    return ResearcherOutput(
        input=input,
        content=content,
        searches_performed=_count_searches(response),
    )
