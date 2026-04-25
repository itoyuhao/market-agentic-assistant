"""Final probe: does running fetchers poison the process for subsequent SDK calls?

Calls SDK BEFORE and AFTER running the macro fetcher, in one process, one
asyncio.run. If BEFORE passes and AFTER fails, we've confirmed that the
fetcher leaves process-level state (file descriptors, signal handlers,
threads…) that breaks anyio.open_process.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402


async def sdk_call(label: str) -> None:
    # Import inside so the module isn't pulled in before the first call.
    from claude_agent_sdk import ClaudeAgentOptions, query

    try:
        count = 0
        async for _ in query(
            prompt="hi",
            options=ClaudeAgentOptions(
                stderr=lambda line: print(f"  [cli] {line}", file=sys.stderr, flush=True),
            ),
        ):
            count += 1
        print(f"  {label}: ✓ {count} messages", file=sys.stderr)
    except Exception as e:
        print(f"  {label}: ✗ {type(e).__name__}: {e}", file=sys.stderr)


async def main() -> None:
    load_dotenv(REPO_ROOT / ".env")

    print("\n=== Step 1: SDK call BEFORE any fetcher runs ===", file=sys.stderr)
    await sdk_call("BEFORE")

    print("\n=== Step 2: Run FRED fetcher (sync, inside async) ===", file=sys.stderr)
    from fetchers.macro import fetch_macro_snapshots

    fetch_macro_snapshots(
        REPO_ROOT / "config" / "macro_indicators.yaml",
        os.getenv("FRED_API_KEY"),
    )
    print("  fetcher done", file=sys.stderr)

    print("\n=== Step 3: SDK call AFTER fetcher ===", file=sys.stderr)
    await sdk_call("AFTER")


if __name__ == "__main__":
    asyncio.run(main())
