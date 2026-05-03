from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from core.models import AnalystInput, AnalystOutput, UniverseRecord
from core.prompts import ANALYST_SYSTEM_PROMPT

_OUTPUT_DIR = Path(__file__).parent.parent / "output"


def _build_user_message(input: AnalystInput) -> str:
    """Serialize universe records into XML for the analyst prompt."""
    parts = ["<pipeline_data>"]

    # Sort by composite score descending so the model sees top signals first
    sorted_records = sorted(
        input.universe,
        key=lambda r: r.composite_score or 0,
        reverse=True,
    )

    for rec in sorted_records:
        # Dump full record as JSON inside a ticker tag
        parts.append(f'  <record ticker="{rec.ticker}" composite_score="{rec.composite_score}">')
        parts.append(f"    {rec.model_dump_json()}")
        parts.append("  </record>")

    parts.append("</pipeline_data>")

    parts.append(f"\nAnalyze the following {len(sorted_records)} records from today's pipeline run.")

    if input.focus_areas:
        parts.append(f"Focus areas: {', '.join(input.focus_areas)}")

    return "\n".join(parts)


def run(input: AnalystInput) -> AnalystOutput:
    """Run the analyst agent."""
    client = anthropic.Anthropic()

    user_message = _build_user_message(input)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        system=ANALYST_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    content = "\n".join(text_blocks)

    # Write report to output directory for debugging
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    report_path = _OUTPUT_DIR / f"report_{ts}.md"
    report_path.write_text(content, encoding="utf-8")

    return AnalystOutput(
        input=input,
        content=content,
        tickers_analyzed=len({r.ticker for r in input.universe}),
    )
