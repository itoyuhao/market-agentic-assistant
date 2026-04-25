"""Verify that claude-agent-sdk can call Claude Code without ANTHROPIC_API_KEY.

Run with:
    python scripts/test_sdk_auth.py

If you see a Traditional Chinese greeting come back, auth works. Delete this
file once confirmed — it's a one-time smoke test.
"""
from __future__ import annotations

import anyio
from claude_agent_sdk import query


async def main() -> None:
    async for msg in query(prompt="請用繁體中文說一句你好，只要一句話。"):
        # msg is a structured Message object; print its repr to see type + content
        print(msg)


if __name__ == "__main__":
    anyio.run(main)
