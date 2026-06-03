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

    # Append research enrichment if available
    if input.research and input.research.researched:
        parts.append("\n<research_data>")
        parts.append("The researcher agent has enriched the following tickers with news,")
        parts.append("sector context, peer companies, and catalyst analysis.")
        parts.append("Use this to ground your catalyst thesis and entry context.\n")

        for tr in input.research.researched:
            parts.append(f'  <research ticker="{tr.ticker}" composite_score="{tr.composite_score}">')
            parts.append(f"    <catalyst_summary>{tr.catalyst_summary}</catalyst_summary>")
            parts.append(f"    <sector_context>{tr.sector_context}</sector_context>")

            if tr.news:
                parts.append("    <news>")
                for n in tr.news:
                    parts.append(f'      <item source="{n.source}">')
                    parts.append(f"        <headline>{n.headline}</headline>")
                    parts.append(f"        <relevance>{n.relevance}</relevance>")
                    if n.url:
                        parts.append(f"        <url>{n.url}</url>")
                    parts.append("      </item>")
                parts.append("    </news>")

            if tr.peers:
                parts.append("    <peers>")
                for p in tr.peers:
                    attrs = f'ticker="{p.ticker}" name="{p.name}"'
                    if p.price is not None:
                        attrs += f' price="{p.price}"'
                    if p.short_interest_pct is not None:
                        attrs += f' si="{p.short_interest_pct}%"'
                    if p.sentiment_bull_pct is not None:
                        attrs += f' bull_pct="{p.sentiment_bull_pct}%"'
                    parts.append(f"      <peer {attrs}>")
                    parts.append(f"        <why>{p.why_relevant}</why>")
                    if p.notable:
                        parts.append(f"        <notable>{p.notable}</notable>")
                    parts.append("      </peer>")
                parts.append("    </peers>")

            if tr.risk_flags:
                parts.append("    <risk_flags>")
                for flag in tr.risk_flags:
                    parts.append(f"      <flag>{flag}</flag>")
                parts.append("    </risk_flags>")

            parts.append("  </research>")

        parts.append("</research_data>")

    parts.append(f"\nAnalyze the following {len(sorted_records)} records from today's pipeline run.")
    if input.research and input.research.researched:
        parts.append(
            f"Research enrichment is provided for {len(input.research.researched)} tickers — "
            "use it to inform your catalyst thesis, entry context, and peer comparisons."
        )

    if input.focus_areas:
        parts.append(f"Focus areas: {', '.join(input.focus_areas)}")

    return "\n".join(parts)


def run(input: AnalystInput) -> AnalystOutput:
    """Run the analyst agent."""
    client = anthropic.Anthropic(max_retries=5)

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
