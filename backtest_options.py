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

"""Stage-2 edge check: Kangaroo grid with REAL option prices (Alpaca data).

Daily-resolution simulation of the Kangaroo grid on SPY in two styles:

  long_options   Variant A: long CALLS (long cluster) / long PUTS (short
                 cluster), buy to open. Expiry = let legs expire (settle at
                 intrinsic, OTM -> total premium loss).
  short_premium  Premium selling: long cluster = cash-secured SHORT PUTS,
                 short cluster = CALL CREDIT SPREADS (naked short calls are
                 not permitted on the account - 403, measured 2026-08-28).
                 OTM expiry keeps the full premium (the seller's win case);
                 ITM expiry settles at intrinsic.
  short_premium_spreads
                 defined-risk premium selling: long cluster = PUT CREDIT
                 SPREADS, short cluster = CALL CREDIT SPREADS. Margin per
                 leg = spread width * 100 - the $100k-account-sized variant
                 (a cash-secured SPY put binds strike*100 = 50-77k per
                 contract, which the contest account cannot stack).

Shared mechanics (ported from the original grid):
  - Decisions once per trading day at the CLOSE on the underlying's close.
  - Rebuy trigger and take-profit threshold from KangarooCore, unchanged.
  - The take-profit check uses the OPEN legs' P&L only; settled (expired)
    legs accumulate in the cluster's sunk pot, booked at cluster end.
  - Regime options at cluster BIRTH (running clusters are never touched):
      mode1        original blind toggle on TP close
      same         restart in the same direction after every cluster end
      sma200       direction from close vs 200-day SMA
      sma200_flat  long-side clusters only above the SMA, flat below
  - No end-of-simulation liquidation; marks are daily trade closes (days
    without trades carry the last close forward and are counted); no
    commissions/fees -> optimistic on spread and costs.
  - Margin metric: cash-secured put = strike*100*qty, call credit spread =
    width*100*qty (max concurrent value reported).

Usage:
  python backtest_options.py [--style short_premium] [--regime same] ...
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
from datetime import date, timedelta

from kangaroo_core import KangarooCore
from backtest_underlying import RANGE_BLOWOUT_PCT, KNOWN_DEFECT_DAYS

DEFAULT_PARAMS = dict(rebuy_1st_pct=1.2, rebuy_pct=0.10, tp_pct=0.10,
                      initial_qty=1, max_invest_count=20, start_long=True)

CLI_PATH = "C:/Users/HMz/Documents/Source/AlpacaTools/cli/alpaca.exe"
ENV_FILE = "C:/Users/HMz/Documents/Source/McpServer/alpaca-mcp-server/dist/env.txt"
CACHE_DIR = "data/option_bars"

SPREAD_WIDTHS = (5, 6, 4, 7, 3, 8, 10)   # credit-spread wing probes, $ from short strike


# --- option daily bar fetcher with on-disk cache -------------------------

def _cli_env() -> dict:
    env = os.environ.copy()
    with open(ENV_FILE, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("ALPACA_API_KEY=") or line.startswith("ALPACA_SECRET_KEY="):
                key, _, value = line.partition("=")
                env[key] = value
    return env


_ENV = None


def fetch_option_closes(occ: str, start: str, end: str) -> dict[str, float]:
    """Daily close per date for one OCC contract, cached on disk."""
    global _ENV
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{occ}.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if _ENV is None:
        _ENV = _cli_env()
    proc = subprocess.run(
        [CLI_PATH, "data", "option", "bars", "--symbols", occ,
         "--timeframe", "1Day", "--start", start, "--end", end,
         "--limit", "1000", "-q"],
        capture_output=True, text=True, env=_ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"option bars {occ} failed: "
                           f"{proc.stdout.strip()} {proc.stderr.strip()}")
    body = json.loads(proc.stdout)
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"option bars {occ}: {body['error']}")
    bars = (body.get("bars") or {}).get(occ) or []
    closes = {b["t"][:10]: b["c"] for b in bars}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(closes, fh)
    os.replace(tmp, path)
    return closes


def occ_symbol(underlying: str, expiry: date, right: str, strike: int) -> str:
    return (f"{underlying}{expiry.strftime('%y%m%d')}"
            f"{'C' if right == 'call' else 'P'}{strike * 1000:08d}")


def pick_contract(underlying: str, entry_day: str, right: str, close: float,
                  dte_min: int, dte_max: int,
                  last_day: str) -> tuple[str, date, float] | None:
    """Nearest weekday expiration in the DTE window, strike nearest to the
    close (probed +-2 around the rounded strike). Returns (occ, expiry,
    entry_close) of the first candidate that traded on the entry day.
    Fetch windows are capped at last_day: Alpaca rejects historical requests
    whose end includes the CURRENT day with 403 'OPRA agreement is not
    signed' (measured 2026-08-28)."""
    d0 = date.fromisoformat(entry_day)
    base = round(close)
    for dte in range(dte_min, dte_max + 1):
        expiry = d0 + timedelta(days=dte)
        if expiry.weekday() >= 5:
            continue
        for strike in (base, base - 1, base + 1, base - 2, base + 2):
            occ = occ_symbol(underlying, expiry, right, strike)
            closes = fetch_option_closes(
                occ, entry_day, min(expiry.isoformat(), last_day))
            if entry_day in closes:
                return occ, expiry, closes[entry_day]
    return None


def pick_spread(underlying: str, entry_day: str, right: str, close: float,
                dte_min: int, dte_max: int, last_day: str):
    """Credit spread: ATM short leg + protective wing (above the short
    strike for calls, below for puts). Iterates expiries AND wing widths -
    a missing wing at one expiry moves on to the next candidate instead of
    aborting. Returns (occ_s, strike_s, closes_s, occ_w, strike_w, closes_w,
    expiry) of the first candidate whose both legs traded on the entry day
    with a positive net credit, or None."""
    d0 = date.fromisoformat(entry_day)
    base = round(close)
    sign = 1 if right == "call" else -1
    for dte in range(dte_min, dte_max + 1):
        expiry = d0 + timedelta(days=dte)
        if expiry.weekday() >= 5:
            continue
        for strike in (base, base - 1, base + 1, base - 2, base + 2):
            occ_s = occ_symbol(underlying, expiry, right, strike)
            closes_s = fetch_option_closes(
                occ_s, entry_day, min(expiry.isoformat(), last_day))
            if entry_day not in closes_s:
                continue
            for width in SPREAD_WIDTHS:
                strike_w = strike + sign * width
                occ_w = occ_symbol(underlying, expiry, right, strike_w)
                closes_w = fetch_option_closes(
                    occ_w, entry_day, min(expiry.isoformat(), last_day))
                if (entry_day in closes_w
                        and closes_s[entry_day] - closes_w[entry_day] > 0):
                    return (occ_s, strike, closes_s,
                            occ_w, strike_w, closes_w, expiry)
    return None


# --- simulation ----------------------------------------------------------

def run(bars: list[dict], params: dict, dte_min: int, dte_max: int,
        underlying: str = "SPY", sma: dict[str, float] | None = None,
        style: str = "long_options", regime: str = "mode1") -> dict:
    core = KangarooCore(**params)
    mult = core.contract_multiplier
    last_day = bars[-1]["t"][:10]

    leg_meta: dict[str, dict] = {}
    sunk_pot = 0.0
    cluster_start = None
    clusters = []
    equity = []
    realized_total = 0.0
    stats = dict(legs_opened=0, legs_expired_otm=0, legs_expired_itm=0,
                 legs_sold_tp=0, stale_marks=0,
                 premium_gross=0.0, max_margin=0.0)

    def leg_pnl(leg) -> float:
        meta = leg_meta[leg.option_symbol]
        if meta["kind"] == "long_option":
            return (meta["last_close"] - leg.entry_premium) * mult * leg.qty
        if meta["kind"] == "short_put":
            return (leg.entry_premium - meta["last_close"]) * mult * leg.qty
        net = meta["last_close"] - meta["wing_last_close"]
        return (leg.entry_premium - net) * mult * leg.qty

    def leg_settle(leg, close: float) -> float:
        meta = leg_meta[leg.option_symbol]
        strike = meta["strike"]
        if meta["kind"] == "long_option":
            intrinsic = (max(close - strike, 0.0) if meta["right"] == "call"
                         else max(strike - close, 0.0))
            if intrinsic > 0:
                stats["legs_expired_itm"] += 1
            else:
                stats["legs_expired_otm"] += 1
            return (intrinsic - leg.entry_premium) * mult * leg.qty
        if meta["kind"] == "short_put":
            intrinsic = max(strike - close, 0.0)
            if intrinsic > 0:
                stats["legs_expired_itm"] += 1
            else:
                stats["legs_expired_otm"] += 1
            return (leg.entry_premium - intrinsic) * mult * leg.qty
        if meta["kind"] == "call_spread":
            net_intrinsic = (max(close - strike, 0.0)
                             - max(close - meta["wing_strike"], 0.0))
        else:                                     # put_spread
            net_intrinsic = (max(strike - close, 0.0)
                             - max(meta["wing_strike"] - close, 0.0))
        if net_intrinsic > 0:
            stats["legs_expired_itm"] += 1
        else:
            stats["legs_expired_otm"] += 1
        return (leg.entry_premium - net_intrinsic) * mult * leg.qty

    def leg_margin(leg) -> float:
        meta = leg_meta[leg.option_symbol]
        if meta["kind"] == "short_put":
            return meta["strike"] * mult * leg.qty
        if meta["kind"].endswith("_spread"):
            return abs(meta["wing_strike"] - meta["strike"]) * mult * leg.qty
        return 0.0

    def end_cluster(kind: str, day: str, result: float,
                    legs_at_end: int) -> None:
        nonlocal sunk_pot, cluster_start, realized_total
        realized_total += result
        clusters.append({
            "cluster_id": core.cluster_id,
            "direction": "long" if core.is_long else "short",
            "start": cluster_start, "end": day, "end_kind": kind,
            "legs_at_end": legs_at_end,
            "result_usd": round(result, 2),
        })
        if kind == "tp" and regime == "mode1":
            core.on_cluster_closed()          # original Mode1 toggle
        else:
            core.legs.clear()
            core.cluster_id += 1
        sunk_pot = 0.0
        cluster_start = None

    def open_leg(day: str, close: float, qty: int) -> None:
        nonlocal cluster_start
        if style == "long_options":
            right = "call" if core.is_long else "put"
            picked = pick_contract(underlying, day, right, close,
                                   dte_min, dte_max, last_day)
            if picked is None:
                raise RuntimeError(f"no tradable {right} on {day}")
            occ, expiry, entry_close = picked
            if not core.legs:
                cluster_start = day
            core.add_leg(occ, qty, close, entry_close)
            leg_meta[occ] = {
                "kind": "long_option", "right": right,
                "expiry": expiry.isoformat(), "strike": int(occ[-8:]) / 1000.0,
                "closes": fetch_option_closes(
                    occ, day, min(expiry.isoformat(), last_day)),
                "last_close": entry_close,
            }
            stats["premium_gross"] += entry_close * mult * qty
        elif style == "short_premium" and core.is_long:  # cash-secured put
            picked = pick_contract(underlying, day, "put", close,
                                   dte_min, dte_max, last_day)
            if picked is None:
                raise RuntimeError(f"no tradable put on {day}")
            occ, expiry, entry_close = picked
            if not core.legs:
                cluster_start = day
            core.add_leg(occ, qty, close, entry_close)   # credit received
            leg_meta[occ] = {
                "kind": "short_put", "right": "put",
                "expiry": expiry.isoformat(), "strike": int(occ[-8:]) / 1000.0,
                "closes": fetch_option_closes(
                    occ, day, min(expiry.isoformat(), last_day)),
                "last_close": entry_close,
            }
            stats["premium_gross"] += entry_close * mult * qty
        else:                                    # credit-spread leg
            right = "put" if core.is_long else "call"
            picked = pick_spread(underlying, day, right, close,
                                 dte_min, dte_max, last_day)
            if picked is None:
                raise RuntimeError(
                    f"no tradable {right} credit spread on {day}")
            occ_s, strike_s, closes_s, occ_w, strike_w, closes_w, expiry = picked
            credit = closes_s[day] - closes_w[day]
            if not core.legs:
                cluster_start = day
            core.add_leg(occ_s, qty, close, credit)      # net credit
            leg_meta[occ_s] = {
                "kind": f"{right}_spread", "right": right,
                "expiry": expiry.isoformat(), "strike": float(strike_s),
                "wing_strike": float(strike_w), "wing_occ": occ_w,
                "closes": closes_s, "last_close": closes_s[day],
                "wing_closes": closes_w, "wing_last_close": closes_w[day],
            }
            stats["premium_gross"] += credit * mult * qty
        stats["legs_opened"] += 1

    for bar in bars:
        day = bar["t"][:10]
        range_pct = (bar["h"] - bar["l"]) / bar["l"] * 100.0
        if range_pct > RANGE_BLOWOUT_PCT:
            if day in KNOWN_DEFECT_DAYS:
                print(f"DATA-DEFECT skip {day}: {KNOWN_DEFECT_DAYS[day]}")
                continue
            raise RuntimeError(f"range blowout on {day}: {range_pct:.1f} %")
        close = bar["c"]

        # 1) expiry settlements
        if core.legs:
            surviving = []
            for leg in core.legs:
                if day >= leg_meta[leg.option_symbol]["expiry"]:
                    sunk_pot += leg_settle(leg, close)
                else:
                    surviving.append(leg)
            core.legs[:] = surviving
            if not core.legs and sunk_pot != 0.0:
                end_cluster("expired", day, sunk_pot, 0)

        # 2) marks + take-profit on OPEN legs only
        closed_today = False
        if core.legs:
            for leg in core.legs:
                meta = leg_meta[leg.option_symbol]
                if day in meta["closes"]:
                    meta["last_close"] = meta["closes"][day]
                else:
                    stats["stale_marks"] += 1
                if meta["kind"] == "call_spread":
                    if day in meta["wing_closes"]:
                        meta["wing_last_close"] = meta["wing_closes"][day]
                    else:
                        stats["stale_marks"] += 1
            open_pnl = sum(leg_pnl(l) for l in core.legs)
            if core.check_close(open_pnl, close, close):
                stats["legs_sold_tp"] += len(core.legs)
                end_cluster("tp", day, sunk_pot + open_pnl, len(core.legs))
                closed_today = True

        # 3) rebuy (never on the day of a close - original ordering)
        if not closed_today:
            skip_open = False
            if not core.legs and regime in ("sma200", "sma200_flat"):
                if sma is None or day not in sma:
                    raise RuntimeError(f"no SMA value for {day}")
                up = close > sma[day]
                if regime == "sma200":
                    core.is_long = up
                else:                     # sma200_flat: long side or nothing
                    core.is_long = True
                    skip_open = not up
            if not skip_open:
                qty = core.check_rebuy(close, close)
                if qty:
                    open_leg(day, close, qty)

        margin = sum(leg_margin(l) for l in core.legs)
        stats["max_margin"] = max(stats["max_margin"], margin)
        open_mark = (sunk_pot + sum(leg_pnl(l) for l in core.legs)
                     if core.legs else 0.0)
        equity.append((day, realized_total, open_mark))

    open_book = None
    if core.legs or sunk_pot:
        open_book = {
            "direction": "long" if core.is_long else "short",
            "start": cluster_start, "legs": core.invest_count,
            "mark_usd": round(equity[-1][2], 2),
        }
    return {"clusters": clusters, "equity": equity, "stats": stats,
            "realized_total": realized_total, "open_book": open_book}


def summarize(result: dict) -> None:
    clusters = result["clusters"]
    stats = result["stats"]
    eq = [r + o for (_, r, o) in result["equity"]]
    peak, max_dd = float("-inf"), 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = min(max_dd, v - peak)

    tp = [c for c in clusters if c["end_kind"] == "tp"]
    exp = [c for c in clusters if c["end_kind"] == "expired"]
    print(f"days: {len(result['equity'])} "
          f"({result['equity'][0][0]} .. {result['equity'][-1][0]})")
    print(f"clusters ended: {len(clusters)}  "
          f"(TP: {len(tp)}, fully expired: {len(exp)})")
    print(f"  TP clusters:      {sum(c['result_usd'] for c in tp):>12,.2f} USD")
    print(f"  expired clusters: {sum(c['result_usd'] for c in exp):>12,.2f} USD")
    print(f"realized total:     {result['realized_total']:>12,.2f} USD")
    print(f"equity max drawdown: {max_dd:,.2f} USD")
    print(f"legs: opened {stats['legs_opened']}, sold@TP {stats['legs_sold_tp']}, "
          f"expired OTM {stats['legs_expired_otm']}, expired ITM {stats['legs_expired_itm']}")
    print(f"premium gross: {stats['premium_gross']:,.2f} USD; "
          f"max margin bound: {stats['max_margin']:,.2f} USD; "
          f"stale marks carried: {stats['stale_marks']}")
    per_year: dict[str, float] = {}
    for c in clusters:
        per_year[c["end"][:4]] = per_year.get(c["end"][:4], 0.0) + c["result_usd"]
    print("result per year:", {y: round(v, 2) for y, v in sorted(per_year.items())})
    per_dir: dict[str, float] = {}
    for c in clusters:
        per_dir[c["direction"]] = per_dir.get(c["direction"], 0.0) + c["result_usd"]
    print("result per direction:", {d: round(v, 2) for d, v in per_dir.items()})
    print("open book at end:", result["open_book"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", default="data/spy_daily.json")
    parser.add_argument("--start", default="2024-02-01")
    parser.add_argument("--dte-min", type=int, default=4)
    parser.add_argument("--dte-max", type=int, default=10)
    parser.add_argument("--style",
                        choices=["long_options", "short_premium",
                                 "short_premium_spreads"],
                        default="long_options")
    parser.add_argument("--regime",
                        choices=["mode1", "same", "sma200", "sma200_flat"],
                        default="mode1")
    parser.add_argument("--out", default="data")
    for key, val in DEFAULT_PARAMS.items():
        parser.add_argument(f"--{key}", type=type(val), default=val)
    args = parser.parse_args()

    with open(args.bars, "r", encoding="utf-8") as fh:
        all_bars = json.load(fh)["bars"]
    bars = [b for b in all_bars if b["t"][:10] >= args.start]
    sma = None
    if args.regime in ("sma200", "sma200_flat"):
        closes = [b["c"] for b in all_bars]
        sma = {}
        for i, b in enumerate(all_bars):
            if i >= 199:
                sma[b["t"][:10]] = sum(closes[i - 199:i + 1]) / 200.0
    params = {k: getattr(args, k) for k in DEFAULT_PARAMS}
    print(f"params: {params}  dte=[{args.dte_min},{args.dte_max}]  "
          f"style={args.style}  regime={args.regime}  days={len(bars)}")

    result = run(bars, params, args.dte_min, args.dte_max,
                 sma=sma, style=args.style, regime=args.regime)
    summarize(result)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "clusters_options.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "cluster_id", "direction", "start", "end", "end_kind",
            "legs_at_end", "result_usd"])
        writer.writeheader()
        writer.writerows(result["clusters"])
    print(f"clusters written: {path}")


if __name__ == "__main__":
    main()
