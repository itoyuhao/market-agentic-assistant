"""NewsAPI fetcher — per-ticker headlines for watchlist entries.

For each watchlist entry that has a ``news_query`` field, queries NewsAPI's
``/everything`` endpoint and returns up to ``max_per_ticker`` headlines from
the trailing ``hours_back`` window.

Design notes
------------
* Per-ticker = 1 HTTP request. Entries without ``news_query`` (typically ETFs)
  are skipped, keeping API usage low.
* NewsAPI free tier: 500 req/day, results delayed ~24h, English sources only.
  The 24h delay is fine for a morning-run digest pattern.
* Per-entry error isolation: one 4xx / 5xx doesn't kill the batch — the error
  is captured in a sentinel ``NewsHeadline`` with only ``matched_ticker`` and
  ``error`` populated.
* Deduplication: same title appearing twice within one ticker's results is
  dropped (NewsAPI sometimes returns near-dupes from syndication).
* Runnable standalone: ``python fetchers/news.py``.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

NEWSAPI_ENDPOINT = "https://newsapi.org/v2/everything"
# NewsAPI free tier delays article indexing by ~24 hours. A 24h window
# therefore falls entirely within the blackout and returns no results — we
# need at least 48h so the 24-48h band yields hits. Extend further if you're
# running this more than once a day and still see empty responses.
DEFAULT_HOURS_BACK = 48
DEFAULT_MAX_PER_TICKER = 3


@dataclass
class NewsHeadline:
    """One news article relevant to a watchlist ticker.

    For error sentinels, ``title``/``source``/``url`` are empty strings and
    ``error`` is non-None.
    """

    title: str
    source: str
    published_at: str   # ISO8601 from NewsAPI (UTC)
    url: str
    matched_ticker: str  # the ticker whose news_query produced this hit
    error: str | None = None


def fetch_headlines(
    watchlist_path: str | Path,
    newsapi_key: str,
    *,
    hours_back: int = DEFAULT_HOURS_BACK,
    max_per_ticker: int = DEFAULT_MAX_PER_TICKER,
) -> list[NewsHeadline]:
    """Fetch headlines for every watchlist entry with a ``news_query``."""
    if not newsapi_key:
        raise ValueError("NEWSAPI_KEY is required")

    with open(watchlist_path) as f:
        config = yaml.safe_load(f)

    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    headlines: list[NewsHeadline] = []
    for market in ("us", "tw"):
        for entry in config.get(market) or []:
            query = entry.get("news_query")
            if not query:
                continue  # ETFs / anything without an explicit query
            try:
                ticker_news = _query_newsapi(
                    query=query,
                    ticker=entry["ticker"],
                    api_key=newsapi_key,
                    since=since,
                    max_results=max_per_ticker,
                )
                headlines.extend(ticker_news)
            except Exception as e:  # noqa: BLE001 — surface any failure
                headlines.append(
                    NewsHeadline(
                        title="",
                        source="",
                        published_at="",
                        url="",
                        matched_ticker=entry["ticker"],
                        error=f"{type(e).__name__}: {e}",
                    )
                )
    return headlines


# -- internals ----------------------------------------------------------------


def _query_newsapi(
    *,
    query: str,
    ticker: str,
    api_key: str,
    since: datetime,
    max_results: int,
) -> list[NewsHeadline]:
    """Call NewsAPI /everything and return up to ``max_results`` deduped hits."""
    params = {
        "q": query,
        # NewsAPI's `from` expects ISO8601; pass without timezone offset for broadest compatibility.
        "from": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "sortBy": "publishedAt",
        "language": "en",
        # IMPORTANT: restrict matching to the title. NewsAPI's default searches
        # title + description + content, which produces massive false-positive
        # noise (e.g. "NVIDIA" appears in AMD / SK Hynix / Google articles
        # tangentially, and "Arm" matches anything with the word). Title-only
        # search leaves us with articles that are actually ABOUT the ticker.
        "searchIn": "title",
        # Fetch a few extra so post-dedup we still have `max_results`.
        "pageSize": max_results + 3,
        "apiKey": api_key,
    }
    resp = httpx.get(NEWSAPI_ENDPOINT, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(
            f"NewsAPI status={data.get('status')}: {data.get('message', 'unknown')}"
        )

    seen_titles: set[str] = set()
    results: list[NewsHeadline] = []
    for article in data.get("articles", []):
        title = (article.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        results.append(
            NewsHeadline(
                title=title,
                source=(article.get("source") or {}).get("name", "-"),
                published_at=article.get("publishedAt", ""),
                url=article.get("url", ""),
                matched_ticker=ticker,
            )
        )
        if len(results) >= max_results:
            break

    return results


# -- standalone runner --------------------------------------------------------


def _print_table(headlines: list[NewsHeadline]) -> None:
    from rich.console import Console
    from rich.table import Table

    # Group by ticker for readability
    by_ticker: dict[str, list[NewsHeadline]] = {}
    for h in headlines:
        by_ticker.setdefault(h.matched_ticker, []).append(h)

    console = Console()
    for ticker, items in by_ticker.items():
        table = Table(title=f"{ticker} — {len(items)} headline(s)", show_lines=False)
        table.add_column("Published (UTC)", no_wrap=True)
        table.add_column("Source", no_wrap=True)
        table.add_column("Title", overflow="fold")
        table.add_column("Error", style="red", overflow="fold")

        for h in items:
            table.add_row(
                h.published_at[:19].replace("T", " ") if h.published_at else "-",
                h.source or "-",
                h.title or "-",
                h.error or "",
            )
        console.print(table)
        console.print()  # spacer


def _main() -> int:
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    api_key = os.getenv("NEWSAPI_KEY")
    if not api_key:
        print("NEWSAPI_KEY missing in .env", file=sys.stderr)
        return 1

    # Count queried tickers up front so we can distinguish "nothing configured"
    # from "queried but got nothing back".
    with open(repo_root / "config" / "watchlist.yaml") as f:
        wl = yaml.safe_load(f)
    queried = [
        e["ticker"]
        for market in ("us", "tw")
        for e in (wl.get(market) or [])
        if e.get("news_query")
    ]

    print(
        f"Fetching headlines for {len(queried)} ticker(s) with news_query: "
        f"{', '.join(queried)}",
        file=sys.stderr,
    )
    headlines = fetch_headlines(
        repo_root / "config" / "watchlist.yaml",
        api_key,
    )

    successful = [h for h in headlines if h.error is None]
    failed = [h for h in headlines if h.error]
    tickers_with_hits = {h.matched_ticker for h in successful}
    tickers_missed = set(queried) - tickers_with_hits - {h.matched_ticker for h in failed}

    if successful:
        _print_table(successful)

    if failed:
        print("\n=== Errors ===", file=sys.stderr)
        for h in failed:
            print(f"  {h.matched_ticker}: {h.error}", file=sys.stderr)

    if tickers_missed:
        print(
            f"\n=== No hits (API OK, 0 articles) ===\n  {', '.join(sorted(tickers_missed))}",
            file=sys.stderr,
        )
        print(
            "  Tip: free-tier NewsAPI has a 24h indexing delay. Try increasing "
            "hours_back past 48h, or widen the query in watchlist.yaml.",
            file=sys.stderr,
        )

    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
