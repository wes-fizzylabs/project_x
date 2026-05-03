from __future__ import annotations

import json
from pathlib import Path

import agent_analyst
from core.models import AnalystInput, AnalystOutput, UniverseRecord


def run_pipeline(
    focus_areas: list[str] | None = None,
    data_dir: str | None = None,
    skip_data_pipeline: bool = False,
) -> AnalystOutput:
    """Run the pipeline: data pipeline -> Analyst.

    Args:
        focus_areas: Optional areas to emphasize in analysis.
        data_dir: Override for pipeline data directory.
        skip_data_pipeline: If True, skip the data pipeline and use existing
                           universe.json (useful for iterating on analysis).
    """
    from market_intel import DATA_DIR
    d = Path(data_dir) if data_dir else DATA_DIR
    universe_path = d / "universe.json"

    # Stage 1: Run market-intel data pipeline
    if not skip_data_pipeline:
        print("[orchestrator] Starting market-intel pipeline")
        from market_intel.pipeline.run import run_pipeline as run_data_pipeline
        run_data_pipeline(data_dir=str(d))
        print("[orchestrator] Market-intel pipeline complete")
    else:
        print("[orchestrator] Skipping data pipeline, using existing data")

    # Load universe
    if not universe_path.exists():
        return AnalystOutput(
            input=AnalystInput(universe=[]),
            content="",
            error=f"No universe data found at {universe_path}",
        )

    with open(universe_path, encoding="utf-8") as f:
        raw_records = json.load(f)

    records = [UniverseRecord(**r) for r in raw_records]
    print(f"[orchestrator] Loaded {len(records)} records from universe")

    # Stage 2: Analyst — LLM synthesizes pipeline data into analysis
    analyst_input = AnalystInput(
        universe=records,
        focus_areas=focus_areas or [],
    )

    print("[orchestrator] Starting analyst")
    analyst_output = agent_analyst.run(analyst_input)

    if not analyst_output.ok:
        print(f"[orchestrator] Analyst failed: {analyst_output.error}")
        return analyst_output

    print(f"[orchestrator] Analyst complete — {analyst_output.tickers_analyzed} tickers analyzed")
    return analyst_output
