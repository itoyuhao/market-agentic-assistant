"""FRED macro indicator fetcher.

Reads the indicator list from ``config/macro_indicators.yaml`` and for each
series computes:

- latest value + date
- 1-month absolute and percentage change
- historical percentile within the last ``history_years`` window (default 10)

Also computes one derived series: ``net_liquidity = WALCL - WTREGEN - RRPONTSYD``
(the core liquidity proxy referenced in the blocktempo article).

Design notes
------------
* Pure data layer. No judgement / no buy-sell calls.
* One bad indicator doesn't poison the rest — failures are captured in the
  ``error`` field of the per-indicator MacroSnapshot and returned.
* Runnable standalone: ``python fetchers/macro.py`` prints a Rich table so
  Kevin can eyeball results without going through the agent layer.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

DEFAULT_HISTORY_YEARS = 10


@dataclass
class MacroSnapshot:
    """Daily snapshot for one macro indicator.

    All numeric fields are optional so that a partial / failed fetch can still
    be represented. When ``error`` is non-None, the caller should treat the
    numeric fields as unreliable regardless of whether they are populated.

    ``unit`` drives display formatting. Supported values:
        'percent', 'index', 'millions_usd', 'billions_usd', or None.
    """

    fred_id: str
    name: str
    chinese_name: str
    category: str
    unit: str | None
    latest_value: float | None
    latest_date: str | None  # ISO8601 YYYY-MM-DD
    value_1m_ago: float | None
    abs_change_1m: float | None
    pct_change_1m: float | None
    historical_percentile: float | None  # 0.0–1.0 over `history_years` window
    error: str | None = None


def fetch_macro_snapshots(
    indicators_path: str | Path,
    fred_api_key: str,
    *,
    history_years: int = DEFAULT_HISTORY_YEARS,
) -> list[MacroSnapshot]:
    """Fetch snapshots for all indicators in the config plus derived net_liquidity.

    Args:
        indicators_path: path to ``config/macro_indicators.yaml``
        fred_api_key: FRED API key
        history_years: lookback window for percentile computation (default 10)

    Returns:
        One MacroSnapshot per indicator in the config, plus one extra for
        the derived net_liquidity series. Order matches the YAML order; the
        derived row is always last.
    """
    # Import here rather than at module top so that callers that only use the
    # dataclass (e.g. tests) don't pay the fredapi import cost.
    from fredapi import Fred

    if not fred_api_key:
        raise ValueError("FRED_API_KEY is required")

    with open(indicators_path) as f:
        config = yaml.safe_load(f)

    fred = Fred(api_key=fred_api_key)

    snapshots: list[MacroSnapshot] = []
    for ind in config.get("indicators", []):
        try:
            snap = _compute_snapshot(fred, ind, history_years)
        except Exception as e:  # noqa: BLE001 — we want to surface any failure
            snap = MacroSnapshot(
                fred_id=ind["id"],
                name=ind["name"],
                chinese_name=ind.get("chinese_name", ind["name"]),
                category=ind.get("category", ""),
                unit=ind.get("unit"),
                latest_value=None,
                latest_date=None,
                value_1m_ago=None,
                abs_change_1m=None,
                pct_change_1m=None,
                historical_percentile=None,
                error=f"{type(e).__name__}: {e}",
            )
        snapshots.append(snap)

    # Derived: net liquidity = WALCL - WTREGEN - RRPONTSYD
    try:
        snapshots.append(_compute_net_liquidity(fred, history_years))
    except Exception as e:  # noqa: BLE001
        snapshots.append(
            MacroSnapshot(
                fred_id="NET_LIQUIDITY",
                name="Net Liquidity (WALCL - WTREGEN - RRPONTSYD)",
                chinese_name="淨流動性（聯準會資產 - TGA - ON RRP）",
                category="liquidity_derived",
                unit="millions_usd",
                latest_value=None,
                latest_date=None,
                value_1m_ago=None,
                abs_change_1m=None,
                pct_change_1m=None,
                historical_percentile=None,
                error=f"{type(e).__name__}: {e}",
            )
        )

    return snapshots


# -- internals ----------------------------------------------------------------


def _compute_snapshot(fred: Any, ind: dict[str, Any], history_years: int) -> MacroSnapshot:
    """Fetch one FRED series and derive snapshot fields."""
    start = (datetime.now() - timedelta(days=365 * history_years)).strftime("%Y-%m-%d")
    series: pd.Series = fred.get_series(ind["id"], observation_start=start)
    series = series.dropna()
    if series.empty:
        raise ValueError(f"No data returned for {ind['id']}")

    latest_value = float(series.iloc[-1])
    latest_date = series.index[-1].strftime("%Y-%m-%d")

    value_1m_ago, abs_change_1m, pct_change_1m = _one_month_change(series, latest_value)
    historical_percentile = float((series < latest_value).mean())

    return MacroSnapshot(
        fred_id=ind["id"],
        name=ind["name"],
        chinese_name=ind.get("chinese_name", ind["name"]),
        category=ind.get("category", ""),
        unit=ind.get("unit"),
        latest_value=latest_value,
        latest_date=latest_date,
        value_1m_ago=value_1m_ago,
        abs_change_1m=abs_change_1m,
        pct_change_1m=pct_change_1m,
        historical_percentile=historical_percentile,
    )


def _compute_net_liquidity(fred: Any, history_years: int) -> MacroSnapshot:
    """Compute net_liquidity = WALCL - WTREGEN - RRPONTSYD, aligned on daily index.

    Unit note: WALCL and WTREGEN are in millions of USD on FRED, but RRPONTSYD
    is in billions. We multiply RRP by 1000 before subtracting to keep everything
    in millions. Historically (e.g. 2023 when RRP was ~$2T) skipping this
    conversion would underweight RRP by 3 orders of magnitude.
    """
    start = (datetime.now() - timedelta(days=365 * history_years)).strftime("%Y-%m-%d")
    walcl = fred.get_series("WALCL", observation_start=start)  # weekly (Wed), millions USD
    tga = fred.get_series("WTREGEN", observation_start=start)  # daily, millions USD
    rrp = fred.get_series("RRPONTSYD", observation_start=start)  # daily (biz), BILLIONS USD

    # Align differing frequencies by forward-filling. sort=True keeps the union
    # index chronological (required so that .iloc[-1] == latest observation).
    df = pd.concat({"walcl": walcl, "tga": tga, "rrp": rrp}, axis=1, sort=True).ffill().dropna()
    if df.empty:
        raise ValueError("Insufficient overlap to compute net_liquidity")

    # Convert RRP from billions → millions to match WALCL / WTREGEN units.
    net = df["walcl"] - df["tga"] - df["rrp"] * 1000

    latest_value = float(net.iloc[-1])
    latest_date = net.index[-1].strftime("%Y-%m-%d")
    value_1m_ago, abs_change_1m, pct_change_1m = _one_month_change(net, latest_value)
    historical_percentile = float((net < latest_value).mean())

    return MacroSnapshot(
        fred_id="NET_LIQUIDITY",
        name="Net Liquidity (WALCL - WTREGEN - RRPONTSYD)",
        chinese_name="淨流動性（聯準會資產 - TGA - ON RRP）",
        category="liquidity_derived",
        unit="millions_usd",
        latest_value=latest_value,
        latest_date=latest_date,
        value_1m_ago=value_1m_ago,
        abs_change_1m=abs_change_1m,
        pct_change_1m=pct_change_1m,
        historical_percentile=historical_percentile,
    )


def _one_month_change(
    series: pd.Series, latest_value: float
) -> tuple[float | None, float | None, float | None]:
    """Return (value_1m_ago, absolute_change, pct_change) — all nullable."""
    cutoff = series.index[-1] - pd.Timedelta(days=30)
    past = series[series.index <= cutoff]
    if past.empty:
        return None, None, None

    value_1m_ago = float(past.iloc[-1])
    abs_change = latest_value - value_1m_ago
    pct_change = (abs_change / value_1m_ago * 100) if value_1m_ago != 0 else None
    return value_1m_ago, abs_change, pct_change


# -- standalone runner --------------------------------------------------------


def _format_value(value: float | None, unit: str | None, *, signed: bool = False) -> str:
    """Format ``value`` for human-friendly display based on its FRED ``unit``.

    Examples:
        _format_value(6707419, "millions_usd")       -> "$6.71T"
        _format_value(0.114,   "billions_usd")       -> "$114.00M"
        _format_value(51480,   "millions_usd",       signed=True) -> "+$51.48B"
        _format_value(18.92,   "index")              -> "18.92"
        _format_value(3.640,   "percent")            -> "3.640"
    """
    if value is None:
        return "-"

    if value < 0:
        sign = "-"
    elif signed:
        sign = "+"
    else:
        sign = ""
    abs_v = abs(value)

    if unit == "percent":
        return f"{sign}{abs_v:,.3f}"
    if unit == "index":
        return f"{sign}{abs_v:,.2f}"
    if unit == "millions_usd":
        # Native scale is millions → collapse to T / B / M.
        if abs_v >= 1e6:
            return f"{sign}${abs_v / 1e6:.2f}T"
        if abs_v >= 1e3:
            return f"{sign}${abs_v / 1e3:.2f}B"
        return f"{sign}${abs_v:.2f}M"
    if unit == "billions_usd":
        # Native scale is billions → collapse to T / B / M.
        if abs_v >= 1e3:
            return f"{sign}${abs_v / 1e3:.2f}T"
        if abs_v >= 1:
            return f"{sign}${abs_v:.2f}B"
        return f"{sign}${abs_v * 1000:.2f}M"
    # Unknown unit — plain number with thousands separator.
    return f"{sign}{abs_v:,.3f}"


def _print_table(snapshots: list[MacroSnapshot]) -> None:
    """Render snapshots as a Rich table for eyeballing."""
    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"Macro Snapshot — {datetime.now():%Y-%m-%d %H:%M}")
    table.add_column("FRED ID")
    table.add_column("中文名", overflow="fold")
    table.add_column("Unit")
    table.add_column("Latest", justify="right")
    table.add_column("Date")
    table.add_column("Δ abs 1M", justify="right")
    table.add_column("Δ % 1M", justify="right")
    table.add_column("10Y %ile", justify="right")
    table.add_column("Error", style="red", overflow="fold")

    for s in snapshots:
        table.add_row(
            s.fred_id,
            s.chinese_name,
            s.unit or "-",
            _format_value(s.latest_value, s.unit),
            s.latest_date or "-",
            _format_value(s.abs_change_1m, s.unit, signed=True),
            f"{s.pct_change_1m:+.2f}%" if s.pct_change_1m is not None else "-",
            f"{s.historical_percentile * 100:.0f}" if s.historical_percentile is not None else "-",
            s.error or "",
        )

    Console().print(table)


def _main() -> int:
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        print("FRED_API_KEY missing in .env", file=sys.stderr)
        return 1

    print("Fetching FRED indicators (this may take ~5-10 seconds)...", file=sys.stderr)
    snapshots = fetch_macro_snapshots(
        repo_root / "config" / "macro_indicators.yaml",
        api_key,
    )
    _print_table(snapshots)

    failed = [s for s in snapshots if s.error]
    if failed:
        print(f"\n{len(failed)} indicator(s) failed — see Error column above.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
