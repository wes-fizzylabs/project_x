"""Narrative intelligence pipeline — Reddit, political trades, SEC 8-K, news."""

from pathlib import Path

# Canonical data directory — shared by the pipeline runner.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
