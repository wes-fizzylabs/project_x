"""Read the static macro-economic calendar and emit upcoming events.

Loads calendar JSON files from narrative_intel/calendars/, filters to
events within a configurable lookahead window, and outputs macro.jsonl
with one record per upcoming event.

Usage (standalone):
    python -m narrative_intel.pipeline.macro                   # writes macro.jsonl
    python -m narrative_intel.pipeline.macro out.jsonl          # custom output path
    python -m narrative_intel.pipeline.macro out.jsonl 90       # 90-day lookahead
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_CALENDARS_DIR = Path(__file__).resolve().parent.parent / "calendars"

# Default: surface events in the next 60 days
_DEFAULT_LOOKAHEAD_DAYS = 60


def load_calendars() -> list[dict]:
    """Load all calendar JSON files from the calendars directory."""
    events: list[dict] = []
    if not _CALENDARS_DIR.exists():
        print(f"  warning: calendars dir not found: {_CALENDARS_DIR}", file=sys.stderr)
        return events

    for path in sorted(_CALENDARS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            events.extend(data.get("events", []))
        except Exception as e:
            print(f"  warning: failed to load {path.name}: {e}", file=sys.stderr)

    return events


def filter_upcoming(
    events: list[dict],
    lookahead_days: int = _DEFAULT_LOOKAHEAD_DAYS,
    as_of: date | None = None,
) -> list[dict]:
    """Filter events to those within the lookahead window from as_of date."""
    today = as_of or date.today()
    upcoming: list[dict] = []

    for event in events:
        try:
            event_date = date.fromisoformat(event["date"])
        except (KeyError, ValueError):
            continue

        days_until = (event_date - today).days

        # Skip past events, keep events within lookahead window
        if days_until < 0 or days_until > lookahead_days:
            continue

        record = {
            "source": "macro_calendar",
            "event": event["event"],
            "date": event["date"],
            "days_until": days_until,
            "impact": event.get("impact", "medium"),
            "sectors_affected": event.get("sectors_affected", []),
            "notes": event.get("notes"),
        }
        upcoming.append(record)

    # Sort by date ascending (nearest first)
    upcoming.sort(key=lambda r: r["date"])
    return upcoming


def run(out_path: str = "macro.jsonl", lookahead_days: int = _DEFAULT_LOOKAHEAD_DAYS) -> int:
    """Run the macro calendar pipeline stage."""
    scanned_at = datetime.now(timezone.utc).isoformat()

    events = load_calendars()
    if not events:
        print("  no calendar events found", file=sys.stderr)
        return 1

    upcoming = filter_upcoming(events, lookahead_days=lookahead_days)

    for r in upcoming:
        r["scanned_at"] = scanned_at

    with open(out_path, "w", encoding="utf-8") as f:
        for r in upcoming:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(
        f"  {len(upcoming)} upcoming events (next {lookahead_days} days) -> {out_path}",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "macro.jsonl"
    lookahead = int(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_LOOKAHEAD_DAYS
    return run(out_path, lookahead)


if __name__ == "__main__":
    raise SystemExit(main())
