"""Scrape the OpenInsider screener table to a JSONL file.

Usage (standalone):
    python -m market_intel.pipeline.scrape              # writes trades.jsonl
    python -m market_intel.pipeline.scrape out.jsonl    # custom output path

Dependencies: httpx, selectolax
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import httpx
from selectolax.parser import HTMLParser

URL = (
    "http://openinsider.com/screener"
    "?s=&o=&pl=1&ph=250&ll=&lh=&fd=7&fdr=&td=0&tdr="
    "&fdlyl=&fdlyh=&daysago=&xp=1&vl=1000&vh="
    "&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0"
    "&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h="
    "&sortcol=0&cnt=500&page=1"
)

# Column order in the `tinytable`
COLUMNS = [
    "flags", "filing_datetime", "trade_date", "ticker", "company_name",
    "insider_name", "insider_title", "trade_type", "price", "qty",
    "owned_after", "delta_own_pct", "value_usd", "ret_1d", "ret_1w",
    "ret_1m", "ret_6m",
]
UA = "openinsider-scraper/0.1 (research use)"


def fetch(url: str = URL) -> str:
    resp = httpx.get(url, headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def parse(html: str) -> list[dict]:
    tree = HTMLParser(html)
    table = tree.css_first("table.tinytable")
    if table is None:
        # No results for the current filters (quiet filing period) — return empty
        return []

    rows: list[dict] = []
    for tr in table.css("tbody tr"):
        tds = tr.css("td")
        if len(tds) != len(COLUMNS):
            continue  # skip malformed rows

        row = {col: tds[i].text(strip=True) for i, col in enumerate(COLUMNS)}

        # Pull the SEC Form 4 URL from the filing-date cell
        filing_link = tds[1].css_first("a")
        row["sec_form4_url"] = filing_link.attributes.get("href") if filing_link else None

        # Pull the insider's share/address detail from the title attr
        insider_link = tds[5].css_first("a")
        row["insider_detail"] = insider_link.attributes.get("title") if insider_link else None

        rows.append(row)

    return rows


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "trades.jsonl"
    html = fetch()
    rows = parse(html)
    scraped_at = datetime.now(timezone.utc).isoformat()

    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            row["scraped_at"] = scraped_at
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"wrote {len(rows)} trades to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
