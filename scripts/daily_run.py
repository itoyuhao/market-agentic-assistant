"""Daily entry point — orchestrates fetchers + digest agent.

Run with:
    python scripts/daily_run.py

Pipeline:
    1. Load config + env (sync)
    2. Fetch macro (FRED), market (yfinance), news (NewsAPI), TW extras (stub) (sync)
    3. Compose digest via Claude (claude-agent-sdk) (async — asyncio.run)
    4. Write digests/YYYY-MM-DD.md (Taipei local date)

Key design note
---------------
Steps 1-2 run in plain sync code BEFORE ``asyncio.run`` is entered. Earlier
versions ran the whole pipeline inside one ``asyncio.run(...)``, which made
the event loop service heavy synchronous HTTP calls (yfinance/fredapi both use
``requests``) for ~15 seconds. That left anyio's event loop in a degraded
state and every subsequent ``anyio.open_process`` (the call that spawns the
``claude`` CLI) failed with a silent exit-code-1 crash. Keeping the fetch
phase out of the async context fixes it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Imports from our own modules come after sys.path tweak.
from agent.digest_agent import compose_digest  # noqa: E402
from fetchers.macro import MacroSnapshot, fetch_macro_snapshots  # noqa: E402
from fetchers.market import TickerSnapshot, fetch_watchlist_snapshots  # noqa: E402
from fetchers.news import NewsHeadline, fetch_headlines  # noqa: E402
from fetchers.twstock_extra import TwStockExtra, fetch_tw_extra  # noqa: E402

TPE = ZoneInfo("Asia/Taipei")


def _timed(label: str, fn, *args, **kwargs):
    """Run ``fn`` synchronously, log elapsed seconds, return result."""
    t0 = time.monotonic()
    print(f"[{datetime.now(TPE):%H:%M:%S}] {label}...", file=sys.stderr)
    result = fn(*args, **kwargs)
    elapsed = time.monotonic() - t0
    print(f"  done in {elapsed:.1f}s", file=sys.stderr)
    return result


def fetch_all() -> tuple[
    list[MacroSnapshot],
    list[TickerSnapshot],
    list[NewsHeadline],
    list[TwStockExtra],
]:
    """Run all fetchers synchronously. Must be called BEFORE entering
    asyncio.run() so the event loop isn't polluted by heavy sync HTTP calls.
    """
    fred_key = os.getenv("FRED_API_KEY")
    newsapi_key = os.getenv("NEWSAPI_KEY")
    missing = [
        n for n, v in (("FRED_API_KEY", fred_key), ("NEWSAPI_KEY", newsapi_key)) if not v
    ]
    if missing:
        raise RuntimeError(f"Missing env: {missing}")

    watchlist_path = REPO_ROOT / "config" / "watchlist.yaml"
    macro_path = REPO_ROOT / "config" / "macro_indicators.yaml"

    macro = _timed("Fetch FRED macro", fetch_macro_snapshots, macro_path, fred_key)
    market = _timed("Fetch yfinance watchlist", fetch_watchlist_snapshots, watchlist_path)
    news = _timed("Fetch NewsAPI headlines", fetch_headlines, watchlist_path, newsapi_key)
    tw_extras = _timed(
        "Fetch TW extras (stub)",
        fetch_tw_extra,
        [t.ticker for t in market if t.market == "tw"],
    )
    return macro, market, news, tw_extras


def print_fetcher_summary(
    macro: list[MacroSnapshot],
    market: list[TickerSnapshot],
    news: list[NewsHeadline],
) -> None:
    macro_failed = [s.fred_id for s in macro if s.error]
    market_failed = [t.ticker for t in market if t.error]
    news_failed = [h.matched_ticker for h in news if h.error]
    print("", file=sys.stderr)
    print("=== Fetcher summary ===", file=sys.stderr)
    print(
        f"  Macro    : {len(macro)} total, {len(macro_failed)} failed {macro_failed or ''}",
        file=sys.stderr,
    )
    print(
        f"  Market   : {len(market)} total, {len(market_failed)} failed {market_failed or ''}",
        file=sys.stderr,
    )
    print(
        f"  News     : {len([h for h in news if not h.error])} good, "
        f"{len(news_failed)} API errors {news_failed or ''}",
        file=sys.stderr,
    )
    print("", file=sys.stderr)


async def compose_only(
    macro: list[MacroSnapshot],
    market: list[TickerSnapshot],
    news: list[NewsHeadline],
    tw_extras: list[TwStockExtra],
    run_time: datetime,
    output_path: Path,
) -> None:
    """Async-only: just the claude-agent-sdk call. A fresh event loop wrapping
    only this function keeps the subprocess spawn isolated from all the sync
    HTTP work done earlier.
    """
    await compose_digest(
        watchlist_snapshots=market,
        macro_snapshots=macro,
        headlines=news,
        tw_extras=tw_extras,
        prompt_path=REPO_ROOT / "agent" / "prompts" / "digest.md",
        output_path=output_path,
        run_time=run_time,
    )


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")

    # -- Sync fetch phase (OUTSIDE asyncio.run) ------------------------------
    try:
        macro, market, news, tw_extras = fetch_all()
    except Exception as e:  # noqa: BLE001
        print(f"✗ Fetcher phase failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print_fetcher_summary(macro, market, news)

    # -- Async compose phase (its own event loop) ----------------------------
    run_time = datetime.now(TPE)
    output_path = REPO_ROOT / "digests" / f"{run_time:%Y-%m-%d}.md"

    print(
        f"[{run_time:%H:%M:%S}] Composing digest via claude-agent-sdk...",
        file=sys.stderr,
    )
    t0 = time.monotonic()
    try:
        asyncio.run(compose_only(macro, market, news, tw_extras, run_time, output_path))
    except Exception as e:  # noqa: BLE001
        print(f"✗ Digest composition failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    elapsed = time.monotonic() - t0
    print(f"  done in {elapsed:.1f}s", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"✓ Digest written → {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
