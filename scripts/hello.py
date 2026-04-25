"""Absolute minimum SDK call — to check if we're rate-limited right now."""
from __future__ import annotations

import asyncio
import sys

from claude_agent_sdk import ClaudeAgentOptions, query


async def main() -> None:
    count = 0
    last_type = ""
    try:
        async for msg in query(
            prompt="hi",
            options=ClaudeAgentOptions(
                stderr=lambda line: print(f"[cli] {line}", file=sys.stderr, flush=True),
            ),
        ):
            count += 1
            last_type = type(msg).__name__
            print(f"  msg {count}: {last_type}", file=sys.stderr)
        print(f"✓ OK — {count} messages, last={last_type}", file=sys.stderr)
    except Exception as e:
        print(f"✗ FAILED — {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
