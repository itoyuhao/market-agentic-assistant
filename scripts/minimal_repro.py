"""Narrow down which option actually kills the CLI.

Runs 5 variants in one fresh process, each a small tweak off the baseline.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

REPO_ROOT = Path(__file__).resolve().parent.parent


async def probe(label: str, **kwargs) -> None:
    print(f"\n=== {label} ===", file=sys.stderr)
    opts = ClaudeAgentOptions(
        stderr=lambda line: print(f"  [cli] {line}", file=sys.stderr, flush=True),
        **kwargs,
    )
    try:
        count = 0
        async for _ in query(prompt="hi", options=opts):
            count += 1
        print(f"  ✓ {count} messages", file=sys.stderr)
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}", file=sys.stderr)


async def main() -> None:
    digest_md = (REPO_ROOT / "agent" / "prompts" / "digest.md").read_text(
        encoding="utf-8"
    )

    # A. Baseline (known to pass per hello.py)
    await probe("A / baseline: no options")

    # B. + short system_prompt
    await probe("B / + short English system_prompt", system_prompt="Be concise.")

    # C. + allowed_tools=[]
    await probe(
        "C / + allowed_tools=[]",
        system_prompt="Be concise.",
        allowed_tools=[],
    )

    # D. + model='sonnet'   ← isolating whether --model flag is the crasher
    await probe(
        "D / + model=sonnet",
        system_prompt="Be concise.",
        allowed_tools=[],
        model="sonnet",
    )

    # E. real digest.md (back to minimal options)
    await probe(
        "E / digest.md as system_prompt, no other options",
        system_prompt=digest_md,
    )

    # F. real digest.md + sonnet model
    await probe(
        "F / digest.md + model=sonnet",
        system_prompt=digest_md,
        model="sonnet",
    )


if __name__ == "__main__":
    asyncio.run(main())
