# Agents Monorepo

Multi-agent market intelligence pipeline. Scrapes insider trades, enriches with options/SI/sentiment data, scores signals, and produces analyst reports via LLM.

## Architecture

```
Orchestrator
  ├── Market-Intel Pipeline (8-stage data pipeline)
  │     └── universe.json (scored records)
  └── Analyst Agent (LLM synthesis)
        └── output/report_<timestamp>.md
```

**Orchestrator** kicks off the data pipeline, waits for completion, loads the scored universe, and passes it to the **Analyst** agent which produces actionable analysis.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies
uv sync

# Set your API key
cp .env.example .env  # or create .env with:
# ANTHROPIC_API_KEY=sk-ant-...
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for the analyst agent |
| `STOCKTWITS_USER` | No | StockTwits credentials for enhanced sentiment |
| `STOCKTWITS_PASS` | No | StockTwits credentials for enhanced sentiment |

## Running

### Full Pipeline + Analyst (end-to-end)

Runs all 8 data pipeline stages, then passes the scored universe to the analyst agent.

```bash
uv run python main.py
```

Analyst reports are saved to `packages/agents/analyst/src/output/`.

### Data Pipeline Only

Runs the 8-stage data pipeline without the analyst. Useful for refreshing data independently.

```bash
uv run market-intel-pipeline
```

**Stages:**
1. Scrape OpenInsider (Form 4 filings)
2. Shape trades (normalize fields, compute urgency scores)
3. Enrich with Yahoo Finance (options chains at 1m/3m/6m, short interest, earnings, sector)
4. StockTwits sentiment
5. StockTwits trending equities
6. Unusual Options Activity scan + merge
7. 13F institutional holders (SEC EDGAR)
8. Enrich non-insider universe records

**Output:** `packages/services/market-intel/src/data/universe.json`

### MCP Server

Exposes pipeline data and live research tools for use by Claude or other MCP clients.

```bash
uv run market-intel-mcp
```

Configured in `.mcp.json` for automatic use with Claude Code. Tools:

| Tool | Description |
|---|---|
| `get_universe(ticker?)` | Read scored pipeline output, optional ticker filter |
| `interpret_record(record)` | Add human-readable labels to a record |
| `market_snapshot(ticker)` | Live Yahoo Finance data (options, SI, earnings) |
| `get_sentiment(ticker)` | Live StockTwits sentiment |
| `run_full_pipeline(force?)` | Execute full pipeline (1hr cooldown) |

### Skip Data Pipeline (iterate on analyst)

To re-run the analyst on existing pipeline data without re-scraping:

```python
# In main.py, set:
skip_data_pipeline=True
```

## Project Structure

```
packages/
  core/src/                    # Shared models and prompts
    core/models/analyst.py     #   AnalystInput, AnalystOutput, UniverseRecord
    core/prompts/analyst.py    #   ANALYST_SYSTEM_PROMPT
  agents/
    orchestrator/src/          # Pipeline orchestration
    analyst/src/               # LLM analyst agent
      output/                  #   Saved reports
    researcher/src/            # Web research agent (future)
  services/
    market-intel/src/          # Data pipeline + MCP server
      data/                    #   Pipeline output files
      market_intel/pipeline/   #   8 pipeline stages
      market_intel/mcp/        #   FastMCP server
```
