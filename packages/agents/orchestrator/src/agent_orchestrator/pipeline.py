from __future__ import annotations

import json
from pathlib import Path

import agent_analyst
import agent_researcher
from core.models import AnalystInput, AnalystOutput, UniverseRecord
from core.models.researcher import ResearcherInput, ResearcherOutput


def run_pipeline(
    focus_areas: list[str] | None = None,
    data_dir: str | None = None,
    skip_data_pipeline: bool = False,
    skip_researcher: bool = False,
    score_threshold: float = 30.0,
    max_peers: int = 5,
) -> AnalystOutput:
    """Run the pipeline: data pipeline -> Researcher -> Analyst.

    Args:
        focus_areas: Optional areas to emphasize in analysis.
        data_dir: Override for pipeline data directory.
        skip_data_pipeline: If True, skip the data pipeline and use existing
                           universe.json (useful for iterating on analysis).
        skip_researcher: If True, skip the researcher stage.
        score_threshold: Minimum composite score for researcher to deep-dive.
        max_peers: Max peer companies per ticker for researcher.
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

    # Stage 2: Researcher — web search + peer discovery
    researcher_output: ResearcherOutput | None = None
    if not skip_researcher:
        researcher_input = ResearcherInput(
            universe=raw_records,
            focus_areas=focus_areas or [],
            score_threshold=score_threshold,
            max_peers=max_peers,
        )

        print("[orchestrator] Starting researcher")
        researcher_output = agent_researcher.run(researcher_input)

        if not researcher_output.ok:
            print(f"[orchestrator] Researcher failed: {researcher_output.error}")
            print("[orchestrator] Continuing to analyst without research data")
            researcher_output = None
        else:
            researched_count = len(researcher_output.researched)
            peer_count = sum(len(r.peers) for r in researcher_output.researched)
            print(
                f"[orchestrator] Researcher complete — {researched_count} tickers researched, "
                f"{peer_count} peers discovered, {researcher_output.total_web_searches} web searches"
            )
    else:
        print("[orchestrator] Skipping researcher stage")

    # Stage 3: Analyst — LLM synthesizes pipeline data + research into analysis
    analyst_input = AnalystInput(
        universe=records,
        focus_areas=focus_areas or [],
        research=researcher_output,
    )

    print("[orchestrator] Starting analyst")
    analyst_output = agent_analyst.run(analyst_input)

    if not analyst_output.ok:
        print(f"[orchestrator] Analyst failed: {analyst_output.error}")
        return analyst_output

    print(f"[orchestrator] Analyst complete — {analyst_output.tickers_analyzed} tickers analyzed")
    return analyst_output
