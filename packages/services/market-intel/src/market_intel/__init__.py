"""Market intelligence pipeline — insider trades, options flow, sentiment, 13F, short interest."""

from pathlib import Path

# Canonical data directory — shared by the pipeline runner and MCP server.
# Lives at packages/services/market-intel/src/data/ regardless of cwd.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
