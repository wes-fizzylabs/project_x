# Macro Calendar Refresh Spec

Use this spec to populate a new year's macro calendar (e.g. `macro_2027.json`).
Pass this file to Claude and say: "Refresh the macro calendar for [YEAR]."

## File Format

Output a JSON file named `macro_YYYY.json` in this directory. Follow the exact
schema used in existing calendar files. Each event needs: `event`, `date`,
`impact`, `sectors_affected`, and `notes`.

## Events to Populate

### 1. FOMC Rate Decisions

**Source:** https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm

- Fetch the page directly — it's public and scrapeable
- The Fed publishes the full year's schedule in advance (usually by June of the prior year)
- Extract all meeting dates for the target year
- The statement is released on the **second day** of each two-day meeting (Wednesday)
- Use the second day (statement day) as the `date`
- Mark which meetings include a **Summary of Economic Projections (SEP)** — these are the ones with the asterisk (*) on the Fed's page. SEP meetings are higher impact because they include the dot plot
- There are typically 7-8 meetings per year (2026 had only 7)

**Notes format:**
- SEP meeting: `"Statement + SEP (dot plot) + press conference"`
- Non-SEP meeting: `"Statement + press conference"`

### 2. CPI Release Dates

**Source:** https://www.bls.gov/schedule/news_release/cpi.htm

The BLS site blocks direct scraping (returns 403). Use this verification ladder:

1. **Try the BLS page first** — if it works, great, use it
2. **Check BLS archive URLs** — for months that have already occurred, the archive URL pattern is `https://www.bls.gov/news.release/archives/cpi_MMDDYYYY.htm`. If the page exists, the date in the URL is the confirmed release date. Try dates in the 10th-14th range for each month
3. **Search for** `"CPI release dates YYYY schedule"` — cross-reference at least two third-party sources:
   - eskisignal.com/cpi-release-dates-YYYY/
   - cpiinflationcalculator.com/cpi-release-schedule/
   - usinflationcalculator.com/inflation/consumer-price-index-release-schedule/
4. **White House official schedule PDF** — search for `"schedule of release dates principal federal economic indicators YYYY" site:whitehouse.gov`. This PDF is authoritative but may not parse well

**Typical pattern:** CPI is released on a Tuesday or Wednesday in the second week of each month (10th-14th), at 8:30 AM ET. Each release covers the prior month's data.

**Notes format:** `"[Month] CPI"` with verification status, e.g.:
- Verified: `"April CPI — verified via BLS archive"`
- Unverified: `"August CPI — from eskisignal, verify against BLS"`

### 3. Nonfarm Payrolls (Employment Situation)

**Source:** https://www.bls.gov/schedule/news_release/empsit.htm

Same 403 issue as CPI. Use this verification ladder:

1. **Try the BLS page first**
2. **Check BLS archive URLs** — pattern is `https://www.bls.gov/news.release/archives/empsit_MMDDYYYY.htm`. Try the first Friday of each month
3. **Search for** `"nonfarm payrolls release dates YYYY"` — cross-reference:
   - forexdailyinfo.com/non-farm-payroll-dates/
   - fxstreet.com NFP calendar
   - tradingcalendar.com/nfp
4. **White House PDF** (same as CPI above)

**Typical pattern:** First Friday of each month at 8:30 AM ET. Exceptions:
- If the first Friday falls on/near July 4, it may shift to Thursday
- If the first Friday falls on/near Jan 1, it shifts to the second Friday
- Government shutdowns can delay releases

**Notes format:** `"[Month] jobs"` with any anomalies noted, e.g.:
- Normal: `"May jobs"`
- Anomaly: `"June jobs — Thursday release (July 4 weekend)"`

## Verification Checklist

After populating the file, verify:

- [ ] FOMC: All dates are Wednesdays (statement day)
- [ ] FOMC: SEP meetings are correctly marked (typically 4 per year: Mar, Jun, Sep, Dec)
- [ ] FOMC: Total meeting count is 7-8
- [ ] CPI: All dates fall between the 9th-15th of each month
- [ ] CPI: 12 releases total (one per month)
- [ ] NFP: Most dates are Fridays (note any exceptions)
- [ ] NFP: 12 releases total (one per month)
- [ ] No duplicate dates across any event type
- [ ] Run the smoke test: `uv run python -c "from narrative_intel.pipeline.macro import load_calendars, filter_upcoming; from datetime import date; events = load_calendars(); upcoming = filter_upcoming(events, 365, date(YYYY, 1, 1)); print(f'{len(upcoming)} events'); [print(f'  {e[\"date\"]}  {e[\"event\"]}') for e in upcoming]"`
- [ ] Total event count should be ~31 (7-8 FOMC + 12 CPI + 12 NFP)

## Confidence Tagging

Tag each event's `notes` with verification status so future refreshes know what's confirmed:
- `"verified via BLS archive"` — confirmed by official archive URL
- `"verified via federalreserve.gov"` — confirmed on Fed website
- `"confirmed via BLS next-release note"` — the prior month's report stated this as the next date
- `"from [source]"` — third-party source, not yet verified against BLS
- `"estimated, verify against BLS"` — based on historical pattern only
