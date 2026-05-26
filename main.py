import os
from pathlib import Path

from agent_orchestrator import run_pipeline

# Load .env file from repo root
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main():
    result = run_pipeline(
        focus_areas=["insider trading patterns", "squeeze setups"],
        skip_data_pipeline=True,
    )

    if result.ok:
        print("\n--- Analysis Output ---\n")
        print(result.content)
        print(f"\n[{result.tickers_analyzed} tickers analyzed]")
    else:
        print(f"Pipeline failed: {result.error}")


if __name__ == "__main__":
    main()
