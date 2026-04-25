"""Isolate the claude-agent-sdk failure by running three progressively
restrictive configurations. Whichever one first breaks tells us the culprit.

Run with:
    python scripts/debug_sdk.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

REPO_ROOT = Path(__file__).resolve().parent.parent


def _stderr_cb(line: str) -> None:
    print(f"[cli-stderr] {line}", file=sys.stderr)


async def test(label: str, options: ClaudeAgentOptions, prompt: str) -> bool:
    """Return True on success, False on failure. Prints outcome."""
    print(f"\n=== {label} ===", file=sys.stderr)
    print(f"prompt_len={len(prompt)} chars", file=sys.stderr)
    try:
        n_messages = 0
        async for msg in query(prompt=prompt, options=options):
            n_messages += 1
            # Don't print whole message — just count
        print(f"✓ PASSED — received {n_messages} messages", file=sys.stderr)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"✗ FAILED — {type(e).__name__}: {e}", file=sys.stderr)
        return False


async def main() -> int:
    # Test 1: Defaults (known to work — matches test_sdk_auth.py)
    await test(
        "Test 1 / DEFAULTS (baseline — should pass)",
        ClaudeAgentOptions(stderr=_stderr_cb),
        "Say hi in one word.",
    )

    # Test 2: Our isolation options + tiny prompt
    await test(
        "Test 2 / OUR OPTIONS + tiny prompt",
        ClaudeAgentOptions(
            system_prompt="You are a concise assistant.",
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi in one word.",
    )

    # Test 3: Our isolation options + the actual digest-sized prompt
    large_prompt = "x" * 30_000 + "\n\nSay hi in one word."
    await test(
        "Test 3 / OUR OPTIONS + ~30k-char prompt",
        ClaudeAgentOptions(
            system_prompt="You are a concise assistant.",
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        large_prompt,
    )

    # Test 4: Default setting_sources + our other options (isolate setting_sources)
    await test(
        "Test 4 / setting_sources dropped (to isolate)",
        ClaudeAgentOptions(
            system_prompt="You are a concise assistant.",
            allowed_tools=[],
            stderr=_stderr_cb,
        ),
        "Say hi in one word.",
    )

    # -- NEW: reproduce the actual digest call step-by-step ------------------
    # Build the exact same prompts the digest agent would build.
    import os

    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")

    from agent.digest_agent import _build_user_prompt
    from fetchers.macro import fetch_macro_snapshots
    from fetchers.market import fetch_watchlist_snapshots
    from fetchers.news import fetch_headlines
    from fetchers.twstock_extra import fetch_tw_extra
    from datetime import datetime
    from zoneinfo import ZoneInfo

    print("\n[build] fetching real data to reproduce digest prompt...", file=sys.stderr)
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

    system_prompt = (REPO_ROOT / "agent" / "prompts" / "digest.md").read_text(
        encoding="utf-8"
    )
    user_prompt = _build_user_prompt(
        run_time=datetime.now(ZoneInfo("Asia/Taipei")),
        watchlist=market,
        macro=macro,
        headlines=news,
        tw_extras=tw_extras,
    )
    print(
        f"[build] system_prompt={len(system_prompt)} chars, "
        f"user_prompt={len(user_prompt)} chars",
        file=sys.stderr,
    )

    # Test 5: Actual system_prompt + tiny user prompt
    await test(
        "Test 5 / REAL system_prompt + tiny user",
        ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi.",
    )

    # Test 6: Tiny system prompt + actual user_prompt
    await test(
        "Test 6 / tiny system + REAL user_prompt",
        ClaudeAgentOptions(
            system_prompt="You are a test assistant.",
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        user_prompt,
    )

    # Test 7: Full reproduction
    await test(
        "Test 7 / REAL system + REAL user (= actual digest call)",
        ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        user_prompt,
    )

    # -- Isolate what in digest.md kills the CLI -----------------------------

    # Test 8: Chinese-only system prompt, no markdown fences, no financial terms
    await test(
        "Test 8 / Chinese system, neutral (no fences, no finance terms)",
        ClaudeAgentOptions(
            system_prompt="你是一個簡短的助理。請用繁體中文回答。回答限 10 個字以內。",
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi.",
    )

    # Test 9: English, same structure as digest.md (with markdown + fences)
    await test(
        "Test 9 / English system with markdown fences",
        ClaudeAgentOptions(
            system_prompt=(
                "# System\n\nYou are a market digest assistant.\n\n"
                "## Format\n\nOutput as:\n\n```\n## Section 1\n...\n```\n"
            ),
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi.",
    )

    # Test 10: Chinese, no markdown, but WITH financial terms
    await test(
        "Test 10 / Chinese + finance terms (no markdown)",
        ClaudeAgentOptions(
            system_prompt=(
                "你是一個市場資訊聚合助理。職責：整理美股、台股、宏觀指標。"
                "絕對不做買賣建議。"
            ),
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi.",
    )

    # Test 11: Actual digest.md loaded as a file via SystemPromptFile
    # (bypasses CLI argv parsing — if this works, the issue is argv handling
    # of the multiline+CJK content)
    await test(
        "Test 11 / digest.md as SystemPromptFile (bypass argv)",
        ClaudeAgentOptions(
            system_prompt={
                "type": "file",
                "path": str(REPO_ROOT / "agent" / "prompts" / "digest.md"),
            },
            allowed_tools=[],
            setting_sources=["project"],
            stderr=_stderr_cb,
        ),
        "Say hi.",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
