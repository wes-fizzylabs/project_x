"""Scrape Reddit for ticker mentions, sentiment, and narrative signals.

Sweeps hot/rising posts from target subreddits, extracts ticker mentions,
scores per-post sentiment, and aggregates per-ticker stats. Outputs
reddit.jsonl with one record per discovered ticker.

Auth: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET env vars.
Optionally set REDDIT_USER_AGENT (defaults to a reasonable value).

Usage (standalone):
    python -m narrative_intel.pipeline.reddit                  # writes reddit.jsonl
    python -m narrative_intel.pipeline.reddit out.jsonl         # custom output path
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

# --- config ---

_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
_USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "narrative-intel/0.1 (market research pipeline)",
)

# Subreddits to sweep, ordered by priority
_SUBREDDITS = [
    "wallstreetbets",
    "options",
    "shortsqueeze",
    "stocks",
    "pennystocks",
]

# How many posts to pull per subreddit (hot + rising)
_POSTS_PER_SUB = 50

# Ticker extraction: $AAPL, $TSLA, or bare 2-5 letter uppercase words
# that look like tickers (filtered against a stopword list)
_TICKER_RE = re.compile(
    r"""
    \$([A-Z]{1,5})\b           # explicit $TICKER
    |                           # or
    (?<![a-zA-Z])([A-Z]{2,5})(?![a-zA-Z])  # bare uppercase 2-5 chars
    """,
    re.VERBOSE,
)

# Common English words that look like tickers — filter these out
_TICKER_STOPWORDS = {
    "I", "A", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE",
    "IF", "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR",
    "SO", "TO", "UP", "US", "WE", "DD", "CEO", "CFO", "COO", "CTO",
    "ETF", "IPO", "SEC", "FDA", "EPS", "ATH", "ATL", "IMO", "YOLO",
    "FYI", "RN", "OP", "PM", "THE", "AND", "FOR", "ARE", "BUT", "NOT",
    "YOU", "ALL", "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "DAY",
    "GET", "HAS", "HIM", "HIS", "HOW", "ITS", "MAY", "NEW", "NOW",
    "OLD", "SEE", "WAY", "WHO", "DID", "GOT", "LET", "SAY", "SHE",
    "TOO", "USE", "LOL", "WTF", "SMH", "TBH", "IMO", "EOD", "AMA",
    "EDIT", "THIS", "THAT", "WITH", "HAVE", "FROM", "THEY", "BEEN",
    "WILL", "JUST", "LIKE", "SOME", "THAN", "THEM", "VERY", "WHEN",
    "WHAT", "OVER", "ALSO", "BACK", "MUCH", "THEN", "HERE", "ONLY",
    "COME", "MAKE", "WELL", "INTO", "LONG", "SHORT", "HOLD", "SELL",
    "CALL", "PUTS", "BEAR", "BULL", "MOON", "HODL", "FOMO", "TLDR",
    "GAIN", "LOSS", "DUMP", "PUMP", "RISK", "SAFE", "DEBT", "CASH",
    "FREE", "HIGH", "DOWN", "MOVE", "NEXT", "PLAY", "OPEN", "WEEK",
    "YEAR", "WANT", "NEED", "LOOK", "BEEN", "DONT", "CANT", "MOST",
    "SURE", "REAL", "GOOD", "BEST", "POST", "THINK", "GOING", "COULD",
    "STILL", "AFTER", "ABOUT", "WOULD", "EVERY", "THEIR", "OTHER",
    "WHICH", "THOSE", "THESE", "BEING", "RIGHT", "WHERE", "THERE",
    "NEVER", "FIRST", "MONEY", "TODAY", "SHARE", "STOCK", "PRICE",
    "POINT", "TRADE", "GREEN", "MARKET", "PUTS",
}

# Bullish / bearish keyword heuristics
_BULLISH_KEYWORDS = {
    "bullish", "moon", "rocket", "calls", "long", "buy", "buying",
    "undervalued", "squeeze", "breakout", "ripping", "tendies", "lambo",
    "gamma", "mooning", "explosive", "upside", "accumulating", "loaded",
}
_BEARISH_KEYWORDS = {
    "bearish", "puts", "short", "sell", "selling", "overvalued", "dump",
    "crash", "drilling", "tanking", "bag", "bagholder", "rug", "scam",
    "downside", "fade", "dead", "avoid", "dropping", "collapsing",
}


# --- auth ---

_token_cache: dict[str, str | float] = {}


def _get_access_token(client: httpx.Client) -> str:
    """Obtain an OAuth2 bearer token using client credentials (script app)."""
    now = time.time()
    if _token_cache.get("token") and now < _token_cache.get("expires_at", 0):
        return _token_cache["token"]  # type: ignore[return-value]

    resp = client.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(_CLIENT_ID, _CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    return token


def _api_get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    """Authenticated GET against the Reddit OAuth API."""
    token = _get_access_token(client)
    resp = client.get(
        f"https://oauth.reddit.com{path}",
        params=params or {},
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


# --- ticker extraction ---

def extract_tickers(text: str) -> set[str]:
    """Extract plausible stock tickers from text."""
    matches = _TICKER_RE.findall(text)
    tickers: set[str] = set()
    for explicit, bare in matches:
        t = explicit or bare
        if t and t not in _TICKER_STOPWORDS and len(t) >= 2:
            tickers.add(t)
    return tickers


# --- sentiment scoring ---

def score_post_sentiment(title: str, body: str, flair: str | None) -> str:
    """Simple keyword-based sentiment: bullish / bearish / neutral."""
    text = f"{title} {body} {flair or ''}".lower()

    # Check flair first — subreddits like WSB have explicit flairs
    if flair:
        flair_lower = flair.lower()
        if any(k in flair_lower for k in ("gain", "yolo", "bull", "dd")):
            return "bullish"
        if any(k in flair_lower for k in ("loss", "bear", "puts")):
            return "bearish"

    bull_hits = sum(1 for kw in _BULLISH_KEYWORDS if kw in text)
    bear_hits = sum(1 for kw in _BEARISH_KEYWORDS if kw in text)

    if bull_hits > bear_hits:
        return "bullish"
    if bear_hits > bull_hits:
        return "bearish"
    return "neutral"


# --- scraping ---

def fetch_subreddit_posts(
    client: httpx.Client,
    subreddit: str,
    limit: int = _POSTS_PER_SUB,
) -> list[dict]:
    """Fetch hot + rising posts from a subreddit, deduplicated."""
    posts: dict[str, dict] = {}

    for listing in ("hot", "rising"):
        try:
            data = _api_get(client, f"/r/{subreddit}/{listing}", {"limit": limit})
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                post_id = post.get("id")
                if post_id and post_id not in posts:
                    posts[post_id] = post
        except Exception as e:
            print(f"  {subreddit}/{listing} error: {e}", file=sys.stderr)

    return list(posts.values())


def parse_post(post: dict, subreddit: str) -> dict:
    """Parse a raw Reddit post into a structured record."""
    title = post.get("title", "")
    body = post.get("selftext", "") or ""
    flair = post.get("link_flair_text")
    created_utc = post.get("created_utc", 0)

    tickers = extract_tickers(f"{title} {body}")
    sentiment = score_post_sentiment(title, body, flair)

    return {
        "post_id": post.get("id"),
        "subreddit": subreddit,
        "title": title,
        "flair": flair,
        "tickers": sorted(tickers),
        "sentiment": sentiment,
        "score": post.get("score", 0),
        "upvote_ratio": post.get("upvote_ratio", 0),
        "num_comments": post.get("num_comments", 0),
        "is_dd": bool(flair and "dd" in flair.lower()),
        "created_utc": datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat() if created_utc else None,
        "url": f"https://reddit.com{post.get('permalink', '')}",
    }


def aggregate_by_ticker(posts: list[dict]) -> list[dict]:
    """Aggregate parsed posts into per-ticker summary records."""
    ticker_data: dict[str, dict] = {}

    for post in posts:
        for ticker in post["tickers"]:
            if ticker not in ticker_data:
                ticker_data[ticker] = {
                    "ticker": ticker,
                    "source": "reddit",
                    "mentions": 0,
                    "subreddits": set(),
                    "posts": [],
                    "bullish": 0,
                    "bearish": 0,
                    "neutral": 0,
                    "total_score": 0,
                    "total_comments": 0,
                    "dd_count": 0,
                }

            td = ticker_data[ticker]
            td["mentions"] += 1
            td["subreddits"].add(post["subreddit"])
            td["posts"].append(post)
            td[post["sentiment"]] += 1
            td["total_score"] += post["score"]
            td["total_comments"] += post["num_comments"]
            if post["is_dd"]:
                td["dd_count"] += 1

    # Build output records
    records: list[dict] = []
    for ticker, td in ticker_data.items():
        tagged = td["bullish"] + td["bearish"]
        top_post = max(td["posts"], key=lambda p: p["score"])

        records.append({
            "ticker": ticker,
            "source": "reddit",
            "mentions": td["mentions"],
            "subreddits": sorted(td["subreddits"]),
            "sentiment": {
                "bullish": td["bullish"],
                "bearish": td["bearish"],
                "neutral": td["neutral"],
                "bullish_pct": round(td["bullish"] / tagged * 100, 1) if tagged else None,
                "bearish_pct": round(td["bearish"] / tagged * 100, 1) if tagged else None,
            },
            "engagement": {
                "total_upvotes": td["total_score"],
                "total_comments": td["total_comments"],
                "dd_posts": td["dd_count"],
            },
            "top_post": {
                "title": top_post["title"],
                "subreddit": top_post["subreddit"],
                "score": top_post["score"],
                "num_comments": top_post["num_comments"],
                "sentiment": top_post["sentiment"],
                "url": top_post["url"],
            },
        })

    # Sort by mentions descending
    records.sort(key=lambda r: r["mentions"], reverse=True)
    return records


# --- main ---

def run(out_path: str = "reddit.jsonl") -> int:
    """Run the Reddit scraper pipeline stage."""
    if not _CLIENT_ID or not _CLIENT_SECRET:
        print(
            "warning: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not set, "
            "Reddit scraping will be unavailable",
            file=sys.stderr,
        )
        return 1

    scanned_at = datetime.now(timezone.utc).isoformat()
    all_posts: list[dict] = []

    with httpx.Client() as client:
        for sub in _SUBREDDITS:
            print(f"  scraping r/{sub}...", file=sys.stderr)
            raw_posts = fetch_subreddit_posts(client, sub)
            parsed = [parse_post(p, sub) for p in raw_posts]
            # Only keep posts that mention at least one ticker
            with_tickers = [p for p in parsed if p["tickers"]]
            all_posts.extend(with_tickers)
            print(
                f"    {len(raw_posts)} posts, {len(with_tickers)} with tickers",
                file=sys.stderr,
            )
            time.sleep(0.5)  # rate-limit courtesy

    # Aggregate by ticker
    records = aggregate_by_ticker(all_posts)

    # Add scan timestamp
    for r in records:
        r["scanned_at"] = scanned_at

    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(
        f"wrote {len(records)} tickers from {len(all_posts)} posts -> {out_path}",
        file=sys.stderr,
    )
    return 0


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "reddit.jsonl"
    return run(out_path)


if __name__ == "__main__":
    raise SystemExit(main())
