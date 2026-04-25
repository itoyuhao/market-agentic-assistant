"""US + TW watchlist fetcher (yfinance-based).

For each ticker in ``config/watchlist.yaml`` we pull ~1 year of daily bars and
derive:

- latest close, previous close, day change (absolute + percent)
- today's volume vs 20-day trailing average (surfaces volume anomalies)
- 52-week high / low and current position within that range (0–1)

Design notes
------------
* Pure data layer. No buy/sell signals.
* Per-ticker error isolation: one failing symbol doesn't kill the batch.
  Failures populate ``TickerSnapshot.error`` and leave numeric fields None.
* ~10 tickers take roughly 20–30 seconds serial; batch mode (``yf.download``)
  is faster but has multi-index quirks for mixed-market lists, not worth it
  at this scale.
* Runnable standalone: ``python fetchers/market.py``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf


@dataclass
class TickerSnapshot:
    """Daily snapshot for one ticker."""

    ticker: str                # yfinance symbol, e.g. "NVDA" or "2330.TW"
    name: str
    market: str                # "us" | "tw"
    category: str
    currency: str              # "USD" | "TWD"
    # Price
    close: float | None
    prev_close: float | None
    change_abs: float | None
    change_pct: float | None
    day_high: float | None
    day_low: float | None
    # Volume
    volume: int | None
    avg_volume_20d: float | None
    volume_ratio: float | None  # volume / avg_volume_20d; >1.5 suggests unusual day
    # 52-week context
    week52_high: float | None
    week52_low: float | None
    week52_position: float | None  # 0.0–1.0 within 52W range
    # Metadata
    latest_date: str | None    # ISO8601 YYYY-MM-DD of the latest bar
    error: str | None = None


def fetch_watchlist_snapshots(watchlist_path: str | Path) -> list[TickerSnapshot]:
    """Fetch snapshots for every US + TW ticker in the watchlist config.

    Order: all US entries first (in config order), then all TW entries.
    """
    with open(watchlist_path) as f:
        config = yaml.safe_load(f)

    snapshots: list[TickerSnapshot] = []
    for market in ("us", "tw"):
        for entry in config.get(market) or []:
            try:
                snap = _fetch_one(entry, market)
            except Exception as e:  # noqa: BLE001 — surface any failure
                snap = _error_snapshot(entry, market, e)
            snapshots.append(snap)
    return snapshots


# -- internals ----------------------------------------------------------------


def _fetch_one(entry: dict, market: str) -> TickerSnapshot:
    """Fetch 1y history for one ticker and derive snapshot fields."""
    ticker_sym: str = entry["ticker"]

    # auto_adjust=True folds dividends into the Close series so that historical
    # returns are comparable. For display ("today's close") this is a no-op
    # because no future dividends exist beyond today.
    hist: pd.DataFrame = yf.Ticker(ticker_sym).history(period="1y", auto_adjust=True)

    if hist.empty:
        raise ValueError(f"yfinance returned empty DataFrame for {ticker_sym}")

    close = float(hist["Close"].iloc[-1])
    prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
    change_abs, change_pct = _day_change(close, prev_close)

    day_high = float(hist["High"].iloc[-1])
    day_low = float(hist["Low"].iloc[-1])

    volume = int(hist["Volume"].iloc[-1])
    avg_volume_20d, volume_ratio = _volume_stats(hist, volume)

    week52_high, week52_low, week52_position = _week52(hist, close)

    latest_date = hist.index[-1].strftime("%Y-%m-%d")
    currency = "USD" if market == "us" else "TWD"

    return TickerSnapshot(
        ticker=ticker_sym,
        name=entry.get("name", ticker_sym),
        market=market,
        category=entry.get("category", ""),
        currency=currency,
        close=close,
        prev_close=prev_close,
        change_abs=change_abs,
        change_pct=change_pct,
        day_high=day_high,
        day_low=day_low,
        volume=volume,
        avg_volume_20d=avg_volume_20d,
        volume_ratio=volume_ratio,
        week52_high=week52_high,
        week52_low=week52_low,
        week52_position=week52_position,
        latest_date=latest_date,
    )


def _day_change(
    close: float, prev_close: float | None
) -> tuple[float | None, float | None]:
    """Return (absolute_change, percent_change) — all None-safe."""
    if prev_close is None or prev_close == 0:
        return None, None
    change_abs = close - prev_close
    return change_abs, change_abs / prev_close * 100


def _volume_stats(
    hist: pd.DataFrame, today_volume: int
) -> tuple[float | None, float | None]:
    """Return (avg_volume_20d, volume_ratio). 20-day average EXCLUDES today."""
    if len(hist) < 21:
        return None, None
    # iloc[-21:-1] = last 20 trading days before today
    avg_vol = float(hist["Volume"].iloc[-21:-1].mean())
    if avg_vol <= 0:
        return avg_vol, None
    return avg_vol, today_volume / avg_vol


def _week52(
    hist: pd.DataFrame, close: float
) -> tuple[float | None, float | None, float | None]:
    """Return (52W high, 52W low, position 0-1).

    Uses the trailing 252 trading days (= ~1 calendar year of US business days).
    If history is shorter we use whatever is available.
    """
    window = hist.iloc[-252:] if len(hist) >= 252 else hist
    high = float(window["High"].max())
    low = float(window["Low"].min())
    if high <= low:
        return high, low, None
    position = (close - low) / (high - low)
    return high, low, position


def _error_snapshot(entry: dict, market: str, e: Exception) -> TickerSnapshot:
    """Build a fully-None snapshot with the error message attached."""
    return TickerSnapshot(
        ticker=entry["ticker"],
        name=entry.get("name", entry["ticker"]),
        market=market,
        category=entry.get("category", ""),
        currency="USD" if market == "us" else "TWD",
        close=None,
        prev_close=None,
        change_abs=None,
        change_pct=None,
        day_high=None,
        day_low=None,
        volume=None,
        avg_volume_20d=None,
        volume_ratio=None,
        week52_high=None,
        week52_low=None,
        week52_position=None,
        latest_date=None,
        error=f"{type(e).__name__}: {e}",
    )


# -- standalone runner --------------------------------------------------------


def _fmt_price(value: float | None, currency: str) -> str:
    if value is None:
        return "-"
    symbol = "$" if currency == "USD" else "NT$"
    return f"{symbol}{value:,.2f}"


def _fmt_pct(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%" if signed else f"{value:.1f}%"


def _print_table(snapshots: list[TickerSnapshot]) -> None:
    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"Watchlist Snapshot — {datetime.now():%Y-%m-%d %H:%M}")
    table.add_column("Mkt")
    table.add_column("Ticker")
    table.add_column("名稱", overflow="fold")
    table.add_column("Close", justify="right")
    table.add_column("Δ day", justify="right")
    table.add_column("Vol / 20D", justify="right")
    table.add_column("52W pos", justify="right")
    table.add_column("52W lo → hi", justify="right")
    table.add_column("Date")
    table.add_column("Error", style="red", overflow="fold")

    for s in snapshots:
        vol_ratio_text = f"{s.volume_ratio:.2f}x" if s.volume_ratio is not None else "-"
        # Bold in red if unusually heavy volume
        if s.volume_ratio is not None and s.volume_ratio >= 1.5:
            vol_ratio_text = f"[yellow]{vol_ratio_text}[/yellow]"

        pos_text = (
            f"{s.week52_position * 100:.0f}%" if s.week52_position is not None else "-"
        )
        range_text = (
            f"{_fmt_price(s.week52_low, s.currency)} → {_fmt_price(s.week52_high, s.currency)}"
            if s.week52_high is not None
            else "-"
        )

        table.add_row(
            s.market.upper(),
            s.ticker,
            s.name,
            _fmt_price(s.close, s.currency),
            _fmt_pct(s.change_pct),
            vol_ratio_text,
            pos_text,
            range_text,
            s.latest_date or "-",
            s.error or "",
        )

    Console().print(table)


def _main() -> int:
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    print(
        "Fetching watchlist snapshots (10 tickers, ~20-30s serial)...",
        file=sys.stderr,
    )
    snapshots = fetch_watchlist_snapshots(repo_root / "config" / "watchlist.yaml")
    _print_table(snapshots)

    failed = [s for s in snapshots if s.error]
    if failed:
        print(
            f"\n{len(failed)} ticker(s) failed — see Error column above.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
