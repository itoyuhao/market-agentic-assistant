"""Taiwan-specific supplementary data (三大法人買賣超 etc.).

Deferred during MVP — per 2026-04-24 decision, the initial digest will rely on
yfinance's price + volume data alone for TW tickers. When re-enabling, prefer
hitting TWSE's public JSON endpoint directly
(https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALL)
rather than going through the `twstock` package, which has been unreliable.

The dataclass is kept so downstream code (digest_agent) can import it with a
stable type; ``fetch_tw_extra`` returns an empty list so the orchestration
doesn't need to branch on "is this implemented yet".
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TwStockExtra:
    """Extra fields that TWSE provides beyond yfinance."""

    ticker: str                           # "2330" (no .TW suffix)
    foreign_net_buy_shares: int | None    # 外資買賣超（股）
    investment_trust_net_buy: int | None  # 投信買賣超
    dealer_net_buy: int | None            # 自營商買賣超


def fetch_tw_extra(tickers: list[str]) -> list[TwStockExtra]:
    """Fetch 三大法人 buy/sell data for Taiwan tickers.

    Currently a no-op stub. Returns an empty list so the digest orchestrator
    can call this unconditionally without special-casing.
    """
    _ = tickers  # intentionally unused
    return []
