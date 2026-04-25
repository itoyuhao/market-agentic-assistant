"""Compose the daily market digest via claude-agent-sdk.

Takes the structured output of all fetchers, serialises it as a JSON block in
the user prompt, and asks Claude (constrained by ``prompts/digest.md``) to turn
it into a Traditional-Chinese markdown digest.

Isolation note
--------------
Kevin's Claude Code has a ``superpowers`` plugin with a ``SessionStart`` hook
that injects ~22k tokens of "you must use skills" instructions into every SDK
session. It also has ~8 MCP servers connected. Both are irrelevant here and
would pollute the agent's behaviour (besides wasting context budget).

We therefore pass explicit ``ClaudeAgentOptions`` with:
  - ``setting_sources=[]`` — skip user/project/local settings entirely
  - ``skills=[]``          — no skills loaded
  - ``hooks={}``           — no hooks registered
  - ``allowed_tools=[]``   — no tool calls; pure text generation

See the feedback memory ``feedback_agent_sdk_isolation.md`` for background.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

from fetchers.macro import MacroSnapshot
from fetchers.market import TickerSnapshot
from fetchers.news import NewsHeadline
from fetchers.twstock_extra import TwStockExtra

DEFAULT_TZ = ZoneInfo("Asia/Taipei")


async def compose_digest(
    *,
    watchlist_snapshots: list[TickerSnapshot],
    macro_snapshots: list[MacroSnapshot],
    headlines: list[NewsHeadline],
    tw_extras: list[TwStockExtra],
    prompt_path: Path,
    output_path: Path,
    run_time: datetime | None = None,
) -> str:
    """Compose a markdown digest, write it to ``output_path``, and return the text.

    Raises:
        FileNotFoundError: if the system prompt file is missing.
        RuntimeError: if Claude returns an empty response.
    """
    run_time = run_time or datetime.now(DEFAULT_TZ)
    system_prompt = prompt_path.read_text(encoding="utf-8")

    user_prompt = _build_user_prompt(
        run_time=run_time,
        watchlist=watchlist_snapshots,
        macro=macro_snapshots,
        headlines=headlines,
        tw_extras=tw_extras,
    )

    # NOTE on isolation: setting_sources=["project"] was silently crashing the
    # CLI even with plenty of budget remaining — root cause unclear (possibly
    # an interaction between that flag and --system-prompt / --input-format
    # stream-json in CLI 2.1.119). For now we accept the full-defaults cost
    # (~22k cache tokens per run from the superpowers SessionStart hook) to
    # get the pipeline working end-to-end. Revisit once stable.
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model="sonnet",
        allowed_tools=[],
        stderr=lambda line: print(f"[claude-cli] {line}", file=sys.stderr),
    )

    assistant_text = ""
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    assistant_text += block.text

    if not assistant_text.strip():
        raise RuntimeError("Claude returned an empty response")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(assistant_text, encoding="utf-8")
    return assistant_text


# -- prompt construction ------------------------------------------------------


def _build_user_prompt(
    *,
    run_time: datetime,
    watchlist: list[TickerSnapshot],
    macro: list[MacroSnapshot],
    headlines: list[NewsHeadline],
    tw_extras: list[TwStockExtra],
) -> str:
    """Serialise everything into a JSON block Claude can parse unambiguously."""
    us = [asdict(t) for t in watchlist if t.market == "us"]
    tw = [asdict(t) for t in watchlist if t.market == "tw"]

    # Split news into good vs errored for clarity in the prompt.
    good_headlines = [asdict(h) for h in headlines if not h.error]
    errored_headlines = [asdict(h) for h in headlines if h.error]

    data = {
        "run_time_local": run_time.strftime("%Y-%m-%d %H:%M %Z"),
        "run_date": run_time.strftime("%Y-%m-%d"),
        "us_watchlist": us,
        "tw_watchlist": tw,
        "macro_indicators": [asdict(s) for s in macro],
        "news_headlines_48h": good_headlines,
        "news_api_errors": errored_headlines,
        "tw_three_institutional": [asdict(x) for x in tw_extras],  # currently empty stub
        "data_quality": {
            "macro_total": len(macro),
            "macro_failed": sum(1 for s in macro if s.error),
            "watchlist_total": len(watchlist),
            "watchlist_failed": sum(1 for t in watchlist if t.error),
            "news_good_headlines": len(good_headlines),
            "news_api_errors": len(errored_headlines),
            "tw_institutional_available": len(tw_extras) > 0,
        },
    }

    return (
        f"下列 JSON 是今天（{run_time:%Y-%m-%d}）fetcher 抓到的完整市場資料。\n"
        "請嚴格依 system prompt 的章節結構（1. 美股收盤 / 2. 台股盤前 / 3. 宏觀雷達 "
        "/ 4. 今日重點事件 / 5. 新聞 Highlights / 6. 資料品質備註），"
        "產出**繁體中文** markdown digest。\n\n"
        "重點提醒：\n"
        "- 純資訊聚合，不做買賣建議\n"
        "- 若 news headlines 裡有明顯文不對題的（例如 'Intel briefing' 其實是情報簡報），"
        "請在 Highlights 裡過濾掉，不要列出\n"
        "- 若某指標或 ticker 的 error 欄位非空，在章節裡標註 `[資料缺失]`\n\n"
        f"```json\n{json.dumps(data, ensure_ascii=False, indent=2, default=str)}\n```"
    )
