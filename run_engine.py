# MIT License
# Copyright (c) 2026 Heinrich Munz
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""QuantroTrader PythonBacktestRunner entry point for Kangaroo Options.

Thin adapter (protocol copied from Robots/Options/run_engine.py): it maps the
GUI's cTrader-CLI-shaped invocation onto ``backtest_options.run`` and
trans-codes the result into the canonical report schema. It adds NO strategy
logic; the backtest engine is the same code the research CLI runs.

Invocation contract (Services/PythonBacktestRunner.cs::BuildArgs):

    python run_engine.py backtest "_" "<pickedYamlPath>"
        --start=<dd/MM/yyyy> --end=<dd/MM/yyyy> --balance=<N>
        --report-json=<path>
        [--period: TOOLBAR TF, the resolution switch ([HOURLY 29.08.2026]):
         d1/day1 -> 1Day bars (unchanged EOD behaviour), h1/hour1 -> 1Hour
         bars (hourly marks/entries/TP against Alpaca 1Hour option bars;
         the hourly SPY store is fetched+cached on first use). Any other
         TF fails loud.]
        [--symbol/--data-mode/--commission/--spread: ignored - costs are
         modelled per spread execution via the ``cost_usd`` panel parameter]

stdout: per-bar equity stream lines and per-cluster TRADE lines in the
QuantroTrader shapes, trailed by ``| 100.0 %`` and the flat JSON block.
History semantics: one history item per CLUSTER (the grid's unit of result);
entryPrice/closePrice carry the UNDERLYING close when the cluster opened and
when it ended, because the chart's y axis is the underlying and its trade
zoom fits y to those two values.
Report timestamps: daily stamps stay date-anchored 00:00Z (the GUI snaps
them to its 09:00 display convention); hourly stamps carry the NEW YORK
wall clock encoded as UTC epoch - REGEL 0.11 (exchange TZ) + the GUI's
[INTRADAY-TIME] rule (BottomPanelControl.SnapTradeTime renders .UtcDateTime
verbatim for non-midnight stamps).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import traceback
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backtest_options import run as run_options_backtest  # noqa: E402
from backtest_options import DEFAULT_PARAMS  # noqa: E402

BOT_NAME = "KangarooOptions"
DEFAULT_YAML = HERE / "kangaroo_options_python.yaml"

# Panel parameters and their targets. Grid params go into KangarooCore via
# DEFAULT_PARAMS keys; the rest are run() arguments.
CORE_PARAM_TYPES = {"rebuy_1st_pct": float, "rebuy_pct": float,
                    "tp_pct": float, "initial_qty": int,
                    "max_invest_count": int, "max_adverse_pct": float}
RUN_PARAM_TYPES = {"dte_min": int, "dte_max": int, "cost_usd": float,
                   "style": str, "direction": str, "underlying": str,
                   "bars_file": str}

# Panel `direction` -> (simulator regime, start_long): the ONE market-side
# knob, mirroring the original Kangaroo's Direction parameter. Measured on
# SPY 02/2024-08/2026 (short_premium_spreads, daily, after the 2026-08-29
# wing-mark fix): long +2,688 / short -5,298 / mode1 +693 USD.
DIRECTION_MAP = {
    "long":        ("same",        True),   # put-spread grid only
    "short":       ("same",        False),  # call-spread grid only
    "mode1":       ("mode1",       True),   # original blind toggle
    "sma200":      ("sma200",      True),   # side from close vs 200-day SMA
    "sma200_flat": ("sma200_flat", True),   # long side above SMA, else flat
}


def _parse_ddmmyyyy(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d/%m/%Y").date()


def _ms_epoch(stamp: str) -> int:
    """Report epoch for a bar stamp.

    Date-only stamp (daily mode): 00:00:00Z as before - the GUI snaps that
    to its 09:00 display convention for daily bots.
    Full timestamp (hourly mode, Alpaca UTC): converted to the NEW YORK wall
    clock and encoded as UTC epoch, because the GUI renders .UtcDateTime
    verbatim for intraday stamps and house rule REGEL 0.11 wants times in
    the asset's exchange TZ (SPY -> America/New_York)."""
    if len(stamp) == 10:
        d = dt.date.fromisoformat(stamp)
        return int(dt.datetime(d.year, d.month, d.day,
                               tzinfo=dt.timezone.utc).timestamp() * 1000)
    from zoneinfo import ZoneInfo
    utc = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    ny = utc.astimezone(ZoneInfo("America/New_York"))
    wall = ny.replace(tzinfo=dt.timezone.utc)
    return int(wall.timestamp() * 1000)


def _stamp_parts(stamp: str) -> tuple[str, str, str]:
    """(dd.MM.yyyy, HH:mm:ss, iso-for-Time=) of a bar stamp, in the SAME
    convention as _ms_epoch: a date-only stamp stays at midnight (daily
    byte-parity with the pre-hourly output), a full UTC stamp is rendered
    as the New York wall clock (REGEL 0.11 exchange TZ)."""
    if len(stamp) == 10:
        d = dt.date.fromisoformat(stamp)
        return d.strftime("%d.%m.%Y"), "00:00:00", f"{stamp}T00:00:00"
    from zoneinfo import ZoneInfo
    utc = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    ny = utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return ny.strftime("%d.%m.%Y"), ny.strftime("%H:%M:%S"), ny.isoformat()


def _read_param_defaults(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = {}
    for name, spec in (raw.get("parameters") or {}).items():
        out[name] = spec["default"] if isinstance(spec, dict) else spec
    return out


def _resolve_bot_yaml(cbotset_path: str) -> Path:
    if cbotset_path and cbotset_path != "_":
        p = Path(cbotset_path)
        if p.is_file():
            return p
    if not DEFAULT_YAML.is_file():
        raise FileNotFoundError(f"Bot YAML missing: {DEFAULT_YAML}")
    return DEFAULT_YAML


# [HOURLY 29.08.2026] toolbar TF -> bar resolution. Anything else fails loud:
# a TF this engine cannot honour must never silently run as another one.
_PERIOD_TO_TIMEFRAME = {
    "d1": "1Day", "day1": "1Day", "daily": "1Day", "": "1Day",
    "h1": "1Hour", "hour1": "1Hour", "1h": "1Hour",
}


# Longest plausible NYSE non-trading stretch in calendar days (holiday
# cluster + weekend). A request may legitimately START or END on such days
# (2026-01-01 is a holiday; the first bar is 2026-01-02) - demanding a bar
# on the exact calendar edge would hard-fail every such window and make the
# store cache refetch forever. Anything beyond this tolerance IS missing
# provider coverage and stays a hard error.
_COVERAGE_TOLERANCE = dt.timedelta(days=5)


def _covers(first_day: str, last_day: str, start: dt.date, end: dt.date) -> bool:
    return (dt.date.fromisoformat(first_day) - start <= _COVERAGE_TOLERANCE
            and end - dt.date.fromisoformat(last_day) <= _COVERAGE_TOLERANCE)


def _ensure_hour_bars(underlying: str, start: dt.date, end: dt.date) -> Path:
    """Fetch-and-cache the hourly RTH underlying bar store (REGEL 0.8: a
    missing derived artifact is built by its canonical producer, then read).

    Uses the ONE proven paging fetcher (fetch_alpaca_bars.fetch, follows
    next_page_token). Measured 2026-08-29: Alpaca paginates hourly stock
    bars at internal segment boundaries even BELOW the row limit (Jan 2026:
    176 rows, token after 2026-01-16), so any fetch without token-following
    silently loses data - a month-chunk loop offers no protection. Bars are
    filtered to the exchange's own session (09:00 NY up to the day's close
    hour from fetch_alpaca_bars.session_closes) and written atomically as
    {"bars":[...]} like the daily store. Re-fetched only when the requested
    window is not covered.

    The close hour comes from the calendar, not from a constant: on the
    eight early-close days between 2024 and 2026 the exchange shuts at
    13:00 while stock bars keep printing, and option bars do not exist for
    those hours - a fixed 9..15 filter made the simulator fail loud with
    "no tradable call credit spread on 2025-11-28T20:00:00Z"."""
    from fetch_alpaca_bars import fetch, session_closes
    path = HERE / "data" / f"{underlying.lower()}_1h.json"
    if path.is_file():
        body = json.loads(path.read_text(encoding="utf-8"))
        bars = body.get("bars") or []
        if bars and _covers(bars[0]["t"][:10], bars[-1]["t"][:10], start, end):
            return path
        print(f"[run_engine] hourly store {path.name} does not cover "
              f"{start}..{end} - refetching", flush=True)
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    raw = fetch(underlying, "1Hour", start.isoformat(), end.isoformat())
    closes = session_closes(start.isoformat(), end.isoformat())
    out = []
    for b in raw:
        utc = dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        local = utc.astimezone(ny)
        day = local.date().isoformat()
        if day not in closes:      # exchange holiday - no session at all
            continue
        # A bar stamped H covers H..H+1, so it belongs to the session only
        # while H is strictly before the close hour.
        close_hour = int(closes[day].split(":")[0])
        if 9 <= local.hour < close_hour:
            out.append(b)
    if not out:
        raise RuntimeError(f"no RTH hourly bars for {underlying} "
                           f"{start}..{end} from Alpaca")
    out.sort(key=lambda b: b["t"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"bars": out}), encoding="utf-8")
    tmp.replace(path)
    print(f"[run_engine] hourly store written: {path.name} "
          f"({len(out)} RTH bars {out[0]['t'][:10]}..{out[-1]['t'][:10]})",
          flush=True)
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kangaroo-options-run-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bt = sub.add_parser("backtest")
    bt.add_argument("algo_placeholder")
    bt.add_argument("cbotset_path")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end", required=True)
    bt.add_argument("--balance", type=float, required=True)
    bt.add_argument("--report-json", dest="report_json", required=True)
    for flag in ("--symbol", "--period", "--data-mode", "--commission",
                 "--spread"):
        bt.add_argument(flag, default=None)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"[run_engine] ignoring cTrader-only flags: {' '.join(unknown)}",
              flush=True)

    start = _parse_ddmmyyyy(args.start)
    end = _parse_ddmmyyyy(args.end)
    if end < start:
        print(f"ERROR: end {end} is before start {start}", file=sys.stderr)
        return 1

    bot_yaml = _resolve_bot_yaml(args.cbotset_path)
    raw_params = _read_param_defaults(bot_yaml)
    print(f"[run_engine] Bot YAML: {bot_yaml}", flush=True)

    core_params = dict(DEFAULT_PARAMS)
    run_params = {"dte_min": 4, "dte_max": 10, "cost_usd": 0.0,
                  "style": "short_premium_spreads", "direction": "long",
                  "underlying": "SPY", "bars_file": "data/spy_daily.json"}
    for name, value in raw_params.items():
        if name in CORE_PARAM_TYPES:
            core_params[name] = CORE_PARAM_TYPES[name](value)
        elif name in RUN_PARAM_TYPES:
            run_params[name] = RUN_PARAM_TYPES[name](value)
        else:
            raise KeyError(
                f"Parameter '{name}' from the bot YAML has no mapping - an "
                f"unmapped parameter would silently have no effect.")
    direction = str(run_params["direction"]).strip().lower()
    if direction not in DIRECTION_MAP:
        raise KeyError(f"direction '{direction}' unknown - allowed: "
                       f"{', '.join(sorted(DIRECTION_MAP))}")
    regime, start_long = DIRECTION_MAP[direction]
    core_params["start_long"] = start_long

    # [HOURLY 29.08.2026] the toolbar TF is the resolution switch
    period_raw = (args.period or "").strip().lower()
    if period_raw not in _PERIOD_TO_TIMEFRAME:
        print(f"ERROR: period '{args.period}' is not supported - this engine "
              f"runs d1 (daily) or h1 (hourly grid against Alpaca 1Hour "
              f"option bars). Pick TF Day or 1 Hour.", file=sys.stderr)
        return 1
    timeframe = _PERIOD_TO_TIMEFRAME[period_raw]
    print(f"[run_engine] period '{args.period}' -> timeframe {timeframe}",
          flush=True)
    if timeframe == "1Hour" and regime in ("sma200", "sma200_flat"):
        print("ERROR: sma200 regimes are defined on the 200-DAY average; an "
              "hourly bar store would silently turn it into a 200-hour one - "
              "run them on TF Day.", file=sys.stderr)
        return 1

    if timeframe == "1Hour":
        try:
            bars_path = _ensure_hour_bars(run_params["underlying"], start, end)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        bars_path = (HERE / run_params["bars_file"]).resolve()
    if not bars_path.is_file():
        print(f"ERROR: bars file not found: {bars_path}", file=sys.stderr)
        return 1
    all_bars = json.loads(bars_path.read_text(encoding="utf-8"))["bars"]
    first_day = all_bars[0]["t"][:10]
    last_day = all_bars[-1]["t"][:10]
    if not _covers(first_day, last_day, start, end):
        print(f"ERROR: requested window {start}..{end} exceeds the bar store "
              f"{first_day}..{last_day} ({bars_path})", file=sys.stderr)
        return 1
    bars = [b for b in all_bars
            if start.isoformat() <= b["t"][:10] <= end.isoformat()]

    sma = None
    if regime in ("sma200", "sma200_flat"):
        closes = [b["c"] for b in all_bars]
        sma = {b["t"][:10]: sum(closes[i - 199:i + 1]) / 200.0
               for i, b in enumerate(all_bars) if i >= 199}

    balance0 = float(args.balance)
    print(f"[run_engine] {BOT_NAME} {run_params['style']}/{direction} "
          f"on {run_params['underlying']} {start}..{end} | "
          f"balance={balance0:,.0f} | {timeframe} bars={len(bars)}", flush=True)

    trade_no = {"n": 0}

    def on_day(i, total, stamp, realized, open_mark):
        bal = balance0 + realized
        eq = bal + open_mark
        date_s, time_s, iso = _stamp_parts(stamp)
        pct = (i + 1) / total * 100.0 if total else 100.0
        print(f"{date_s} {time_s}.000 | {pct:.1f} % | "
              f"Bal={bal:.2f} Eq={eq:.2f} MinEq={eq:.2f} MaxEq={eq:.2f} "
              f"Time={iso} | 0 pos, {trade_no['n']} trades", flush=True)

    def on_cluster(c):
        trade_no["n"] += 1
        end_d, end_t, _ = _stamp_parts(c["end"])
        open_d, open_t, _ = _stamp_parts(c["start"])
        side = "buy" if c["direction"] == "long" else "sell"
        print(f"{end_d} {end_t}.000 | TRADE | "
              f"id={trade_no['n']} sym={run_params['underlying']} dir={side} "
              f"vol={c['legs_at_end']} in={c['start_price']:.2f} "
              f"out={c['end_price']:.2f} "
              f"net={c['result_usd']:.2f} gross={c['result_usd']:.2f} "
              f"comm=0.00 ret=0.0000 open={open_d} {open_t}.000", flush=True)

    try:
        result = run_options_backtest(
            bars, core_params, run_params["dte_min"], run_params["dte_max"],
            underlying=run_params["underlying"], sma=sma,
            style=run_params["style"], regime=regime,
            cost_usd=run_params["cost_usd"], timeframe=timeframe,
            on_day=on_day, on_cluster=on_cluster)
    except Exception as exc:
        print(f"ERROR: backtest failed: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return 1

    # canonical report -----------------------------------------------------
    points, eq_series, bal_series = [], [], []
    for day_iso, realized, open_mark in result["equity"]:
        bal = balance0 + realized
        eq = bal + open_mark
        points.append({"timestamp": _ms_epoch(day_iso), "balance": bal,
                       "minEquity": eq, "maxEquity": eq})
        bal_series.append(bal)
        eq_series.append(eq)

    def _dd_pct(series):
        peak, worst = float("-inf"), 0.0
        for v in series:
            peak = max(peak, v)
            if peak > 0:
                worst = max(worst, (peak - v) / peak * 100.0)
        return worst

    items = []
    for i, c in enumerate(result["clusters"], start=1):
        items.append({
            "id": i,
            "entryTime": _ms_epoch(c["start"]),
            "closeTime": _ms_epoch(c["end"]),
            "symbol": run_params["underlying"],
            # MARKET side, not the option action. The chart draws this on the
            # UNDERLYING axis (up/green for buy, down/red for sell), and a put
            # credit spread is a bullish position even though its legs are
            # sold. Long cluster = put spreads = buy; short cluster = call
            # spreads = sell.
            "direction": "buy" if c["direction"] == "long" else "sell",
            "volume": float(c["legs_at_end"]),
            # Underlying levels, not premiums: the chart's y axis is the
            # underlying, and its trade zoom folds entry/close into the y fit
            # (ChartControl.xaml.cs FitYToVisibleSlice). A premium - or a 0 -
            # there drags the axis to a meaningless range.
            "entryPrice": c["start_price"], "closePrice": c["end_price"],
            "net": c["result_usd"], "gross": c["result_usd"],
            "commissions": 0.0, "tradeReturn": 0.0,
            "comment": f"cluster {c['cluster_id']} {c['direction']} "
                       f"end={c['end_kind']} legs={c['legs_at_end']}",
        })

    open_items = []
    ob = result["open_book"]
    if ob:
        open_items.append({
            "symbol": run_params["underlying"],
            "direction": "buy" if ob["direction"] == "long" else "sell",
            "volume": float(ob["legs"]), "entryPrice": ob["start_price"],
            "currentPrice": ob["last_price"], "net": float(ob["mark_usd"]),
            "commissions": 0.0, "swap": 0.0,
            "entryTime": _ms_epoch(ob["start"] or result["equity"][-1][0]),
            "label": f"open cluster, {ob['legs']} legs",
        })

    wins = [t["net"] for t in items if t["net"] > 0]
    losses = [t["net"] for t in items if t["net"] < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    realized_total = result["realized_total"]
    open_mark_end = result["equity"][-1][2] if result["equity"] else 0.0

    report = {
        "main": {
            "engine": "python", "bot": BOT_NAME,
            "symbol": run_params["underlying"],
            "period": "h1" if timeframe == "1Hour" else "d1",
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "initialBalance": balance0,
            "endingBalance": balance0 + realized_total,
            "endingEquity": balance0 + realized_total + open_mark_end,
            "netProfit": realized_total,
            "totalTrades": len(items),
            "winningTrades": len(wins), "losingTrades": len(losses),
            "profitFactor": pf,
        },
        "equity": {
            "points": points,
            "maxBalanceDrawdownPercent": _dd_pct(bal_series),
            "maxEquityDrawdownPercent": _dd_pct(eq_series),
            "maxBalanceDrawdownAbsolute": _dd_pct(bal_series) / 100.0 * balance0,
            "maxEquityDrawdownAbsolute": _dd_pct(eq_series) / 100.0 * balance0,
        },
        "positions": {"items": open_items, "count": len(open_items)},
        "history": {"items": items, "count": len(items)},
    }
    report_path = Path(args.report_json)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[run_engine] Canonical report: {report_path}", flush=True)

    print("| 100.0 %", flush=True)
    print(json.dumps({
        "Equity": report["main"]["endingEquity"],
        "NetProfit": report["main"]["netProfit"],
        "TotalTrades": report["main"]["totalTrades"],
        "WinningTrades": report["main"]["winningTrades"],
        "LosingTrades": report["main"]["losingTrades"],
        "MaxEquityDrawdownPercentages": report["equity"]["maxEquityDrawdownPercent"],
        "MaxBalanceDrawdownPercentages": report["equity"]["maxBalanceDrawdownPercent"],
        "ProfitFactor": report["main"]["profitFactor"],
        "Fitness": report["main"]["netProfit"],
    }, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
