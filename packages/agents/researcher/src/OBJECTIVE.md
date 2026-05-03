# Agent: Researcher

## Objective

The Researcher agent is responsible for deep-dive information gathering and contextual enrichment. Where the Searcher casts a wide net, the Researcher follows up on promising leads — pulling detailed financial data, summarizing lengthy documents, and building context around specific companies, sectors, or events.

## Responsibilities

- Perform deep research on specific companies, sectors, or market events flagged by the Searcher or Orchestrator
- Summarize and extract key insights from long-form content (earnings reports, analyst notes, regulatory filings)
- Cross-reference data points across multiple sources to validate claims and identify patterns
- Build structured context packages (company profiles, event timelines, competitive landscapes) for the Analyst
- Track historical context relevant to current market signals

## Inputs

- Filtered and prioritized search results from the Searcher agent
- Specific research directives from the Orchestrator (e.g., "deep dive on $TICKER insider activity")
- Raw content that requires summarization or extraction

## Outputs

- Structured research briefs with citations
- Summarized documents with key takeaways highlighted
- Cross-referenced data sets linking related signals (e.g., insider sells + earnings miss + sentiment shift)
- Enriched entity profiles (company fundamentals, recent news, insider activity)

## Key Considerations

- Focus on depth over breadth — the Searcher handles discovery, the Researcher handles understanding
- Maintain source traceability so the Analyst can verify claims
- Output should be LLM-friendly structured data, not raw text dumps
- Designed for sequential invocation — receives Searcher output via the Orchestrator and passes enriched data forward to the Analyst
