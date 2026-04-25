"""Bisect which part of the digest user_prompt crashes the CLI.

Each probe uses the same minimal options and varies only the user prompt.
Whichever variant crashes first points at the culprit content.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: E402
from agent.digest_agent import _build_user_prompt  # noqa: E402
from fetchers.macro import fetch_macro_snapshots  # noqa: E402
from fetchers.market import fetch_watchlist_snapshots  # noqa: E402
from fetchers.news import fetch_headlines  # noqa: E402
from fetchers.twstock_extra import fetch_tw_extra  # noqa: E402


async def probe(label: str, prompt: str) -> None:
    print(f"\n=== {label} (len={len(prompt)}) ===", file=sys.stderr)
    opts = ClaudeAgentOptions(
        model="sonnet",
        allowed_tools=[],
        stderr=lambda line: print(f"  [cli] {line}", file=sys.stderr, flush=True),
    )
    try:
        count = 0
        async for _ in query(prompt=prompt, options=opts):
            count += 1
        print(f"  ✓ {count} messages", file=sys.stderr)
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}", file=sys.stderr)


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")

    # Build the real user_prompt, and also some isolated variants
    macro = fetch_macro_snapshots(
        REPO_ROOT / "config" / "macro_indicators.yaml",
        os.getenv("FRED_API_KEY"),
    )
    market = fetch_watchlist_snapshots(REPO_ROOT / "config" / "watchlist.yaml")
    news = fetch_headlines(
        REPO_ROOT / "config" / "watchlist.yaml",
        os.getenv("NEWSAPI_KEY"),
    )
    tw_extras = fetch_tw_extra([t.ticker for t in market if t.market == "tw"])

    full_prompt = _build_user_prompt(
        run_time=datetime.now(ZoneInfo("Asia/Taipei")),
        watchlist=market,
        macro=macro,
        headlines=news,
        tw_extras=tw_extras,
    )

    # 1: sanity — tiny prompt should pass
    await probe("1 / tiny", "hi")

    # 2: full prompt (should fail based on prior runs)
    await probe("2 / FULL user_prompt", full_prompt)

    # 3: first 1KB (intro + start of JSON)
    await probe("3 / first 1KB of prompt", full_prompt[:1000])

    # 4: first half
    await probe("4 / first HALF", full_prompt[: len(full_prompt) // 2])

    # 5: second half
    await probe("5 / second HALF", full_prompt[len(full_prompt) // 2 :])

    # 6: intro only (before the ```json fence)
    intro_end = full_prompt.find("```json")
    if intro_end > 0:
        await probe("6 / intro only (no JSON)", full_prompt[:intro_end])

    # 7: JSON block only (inside the fences, no Chinese intro)
    import re
    m = re.search(r"```json\n(.*?)\n```", full_prompt, re.DOTALL)
    if m:
        await probe("7 / JSON only (stripped fences + intro)", m.group(1))


if __name__ == "__main__":
    asyncio.run(main())
