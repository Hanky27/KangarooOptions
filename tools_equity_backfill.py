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

"""Rebuild the account's equity for every past bar, and prove it.

WHY THIS EXISTS
The broker's own intraday equity series is not the number this report
publishes. Measured 2026-09-01: `account.equity` 90,321.33 and
`cash + sum(market_value)` 90,455.33 agreed within 134.00 while the
newest 5-minute history bar read 95,595.88. Read again three minutes
later, eleven of its bars had all moved by exactly -84.00 at once - a
block shift, not marks settling - and the newest still sat 4,016.40 above
the account. Drawing that series next to a headline it disagrees with is
how a curve lies.

Refusing it left the chart with four points, which is not a chart.

WHAT THIS DOES INSTEAD
Equity is not a number that has to be fetched. It is

    equity(t) = cash(t) + sum over held contracts of qty(t) * mark(t) * 100

and every term is recoverable. `cash(t)` follows from the fills, which are
timestamped to the millisecond and carry side, quantity and price, minus
the fees the broker booked up to t. `qty(t)` is the running position from
those same fills. `mark(t)` is a historical option bar, which the CLI
serves for up to 100 symbols at a time.

THE PART THAT MAKES IT PUBLISHABLE
A reconstruction nobody checked is a guess with more steps. The newest
reconstructed point is compared against the equity this report measured
live at the same time and reconciled to the cent. If the two disagree by
more than the tolerance, nothing is written: a curve that cannot reproduce
the number beside it has no business being drawn.

WHY IT DOES NOT RUN ON THIS ACCOUNT
It cannot. Measured 2026-09-01 against the competition account:

    {"error": "OPRA agreement is not signed", "status": 403,
     "path": ".../v1beta1/options/bars?..."}

Latest QUOTES come back fine - that is the indicative feed the agent
trades on - but historical option BARS need an OPRA agreement this
account does not have. So the past cannot be re-marked, by this tool or
any other, and the equity line can only densify forward from the moment
this report started writing its own measurements down.

The file stays because the dead end is worth recording and because the
reconstruction is correct the moment the entitlement exists. Run it and
it will tell you the same 403.

Usage:
    python tools_equity_backfill.py --config config.yaml
    python tools_equity_backfill.py --config config.yaml --write
"""

import argparse
import collections
import datetime as dt
import json
import os
import sys

import yaml

from alpaca_cli import AlpacaCli, AlpacaCliError, in_batches
from tools_perf_snapshot import (fee_timeline, fetch_all_fills, funding,
                                 read_equity_log, realized_by_contract,
                                 split_activities)

CONTRACT_MULTIPLIER = 100
ET = dt.timezone(dt.timedelta(hours=-4))

# How far a reconstructed point may sit from the live measurement made at
# the same moment. A bar's close and a live NBBO midpoint are two prices
# for the same contract, so they never agree exactly; 1 % of starting
# capital is wide enough for that spread over ~100 legs and far below the
# 4,000-5,000 the broker's own series was off by.
CHECK_TOLERANCE_PCT = 1.0


def epoch(stamp: str) -> int:
    return int(dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
               .timestamp())


def signed(fill: dict) -> float:
    """Contracts added to the position: positive for a buy."""
    return float(fill["qty"]) * (1 if fill["side"] == "buy" else -1)


def cash_flow(fill: dict) -> float:
    """What the fill did to cash. Selling to open pays IN."""
    return -signed(fill) * float(fill["price"]) * CONTRACT_MULTIPLIER


def grid(first: int, last: int, step: int = 300) -> list[int]:
    """The bar stamps to reconstruct, aligned to the timeframe."""
    start = first - (first % step) + step
    return list(range(start, last + 1, step))


def fetch_bars(cli: AlpacaCli, symbols: list[str], start: str, end: str,
               timeframe: str) -> dict[str, list[tuple[int, float]]]:
    """Closing price per symbol per bar, paged and batched.

    Batched at the endpoint's own limit of 100 symbols (`--symbols ... with
    a limit of 100`, and the same 400 that killed the agent on 2026-09-01
    if it is exceeded). Paged because 100 symbols over three days at five
    minutes is far past the 1000-row default.
    """
    out: dict[str, list[tuple[int, float]]] = collections.defaultdict(list)
    for batch in in_batches(symbols, 100):
        token = None
        while True:
            cmd = ["data", "option", "bars", "--symbols", ",".join(batch),
                   "--start", start, "--end", end,
                   "--timeframe", timeframe, "--limit", "10000"]
            if token:
                cmd += ["--page-token", token]
            body = cli._run(cmd)
            bars = body.get("bars") or {}
            for symbol, rows in bars.items():
                for row in rows:
                    out[symbol].append((epoch(row["t"]), float(row["c"])))
            token = body.get("next_page_token")
            if not token:
                break
    for symbol in out:
        out[symbol].sort()
    return dict(out)


def marks_at(bars: dict[str, list[tuple[int, float]]], stamps: list[int]
             ) -> dict[str, dict[int, float]]:
    """The last close at or before each stamp, per symbol.

    Carried forward on purpose: an option that did not trade in a
    five-minute bar is still held at its last price. What is NOT carried is
    a price from before the first bar - a contract with no print yet has no
    mark, and the caller is told rather than given a zero.
    """
    out: dict[str, dict[int, float]] = {}
    for symbol, rows in bars.items():
        per: dict[int, float] = {}
        i, last = 0, None
        for stamp in stamps:
            while i < len(rows) and rows[i][0] <= stamp:
                last = rows[i][1]
                i += 1
            if last is not None:
                per[stamp] = last
        out[symbol] = per
    return out


def reconstruct(cli: AlpacaCli, step: int) -> tuple[list[dict], dict]:
    """Every past equity, rebuilt from fills, fees and historical marks."""
    fills = fetch_all_fills(cli)
    if not fills:
        raise AlpacaCliError("no fills - there is nothing to reconstruct")
    account = cli.account()
    bookings = cli.activities(after=account["created_at"][:10])
    fee_rows, transfer_rows = split_activities(bookings)
    fees = fee_timeline(fee_rows)
    _realized, _per, _closed, events = realized_by_contract(fills)
    first_fill = min(e["t"] for e in events)
    start_equity = funding(transfer_rows, first_fill)

    now = epoch(cli.clock()["timestamp"])
    stamps = grid(first_fill, now, step)
    if not stamps:
        raise AlpacaCliError("the window holds no complete bar yet")

    symbols = sorted({f["symbol"] for f in fills})
    window_start = dt.datetime.fromtimestamp(first_fill, dt.timezone.utc)
    bars = fetch_bars(cli, symbols,
                      window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      dt.datetime.fromtimestamp(
                          now, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      f"{step // 60}Min")
    marks = marks_at(bars, stamps)

    # Walk fills and fee bookings forward alongside the bar grid.
    ordered = sorted(fills, key=lambda f: epoch(f["transaction_time"]))
    position: dict[str, float] = collections.defaultdict(float)
    cash = start_equity
    fi, fee_i = 0, 0
    points: list[dict] = []
    unmarked: collections.Counter = collections.Counter()

    for stamp in stamps:
        while fi < len(ordered) and epoch(ordered[fi]["transaction_time"]) <= stamp:
            f = ordered[fi]
            position[f["symbol"]] += signed(f)
            cash += cash_flow(f)
            fi += 1
        while fee_i < len(fees) and fees[fee_i]["t"] <= stamp:
            fee_i += 1
        charged = fees[fee_i - 1]["fees"] if fee_i else 0.0

        held = 0.0
        for symbol, qty in position.items():
            if not qty:
                continue
            price = marks.get(symbol, {}).get(stamp)
            if price is None:
                unmarked[symbol] += 1
                continue
            held += qty * price * CONTRACT_MULTIPLIER

        realized_now = 0.0
        for e in events:
            if e["t"] <= stamp:
                realized_now = e["realized"]
            else:
                break
        points.append({"t": stamp,
                       "equity": round(cash - charged + held, 2),
                       "balance": round(start_equity + realized_now - charged, 2),
                       "realized": round(realized_now, 2),
                       "fees": round(charged, 2),
                       "source": "reconstructed"})

    return points, {"stamps": len(stamps), "symbols": len(symbols),
                    "bars": sum(len(v) for v in bars.values()),
                    "unmarked": dict(unmarked.most_common(5)),
                    "start_equity": start_equity}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--equity-log", default="state/equity_log.jsonl")
    ap.add_argument("--step-seconds", type=int, default=300)
    ap.add_argument("--write", action="store_true",
                    help="merge the reconstruction into the equity log")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cli = AlpacaCli(cfg["cli_path"], cfg["env_file"])

    points, info = reconstruct(cli, args.step_seconds)
    print(f"{info['stamps']} bars over {info['symbols']} contracts, "
          f"{info['bars']} option bars read")
    if info["unmarked"]:
        print(f"  contracts without a print at some bar: {info['unmarked']}")

    # The check that makes it publishable: against what was measured live.
    measured = [r for r in read_equity_log(args.equity_log)
                if r.get("source") != "reconstructed"]
    if not measured:
        raise SystemExit(
            "no live measurement in the equity log to check against - "
            "refusing to write a reconstruction nobody can verify")
    limit = info["start_equity"] * CHECK_TOLERANCE_PCT / 100.0
    worst = None
    for row in measured:
        near = min(points, key=lambda p: abs(p["t"] - row["t"]))
        if abs(near["t"] - row["t"]) > args.step_seconds:
            continue
        gap = abs(near["equity"] - row["equity"])
        stamp = dt.datetime.fromtimestamp(row["t"], ET).strftime("%m-%d %H:%M")
        print(f"  {stamp} ET  live {row['equity']:>12,.2f}   "
              f"rebuilt {near['equity']:>12,.2f}   {near['equity'] - row['equity']:>+9,.2f}")
        if worst is None or gap > worst:
            worst = gap
    if worst is None:
        raise SystemExit("no live point falls inside the reconstructed grid")
    print(f"\nworst disagreement {worst:,.2f} against {limit:,.2f} allowed")
    if worst > limit:
        raise SystemExit(
            f"the reconstruction cannot reproduce what was measured live "
            f"({worst:,.2f} > {limit:,.2f}) - not writing it")

    if not args.write:
        print("check only - nothing written")
        return 0

    keep = {r["t"]: r for r in points}
    keep.update({r["t"]: r for r in measured})        # live always wins
    os.makedirs(os.path.dirname(args.equity_log) or ".", exist_ok=True)
    with open(args.equity_log, "w", encoding="utf-8") as fh:
        for t in sorted(keep):
            fh.write(json.dumps(keep[t], sort_keys=True) + "\n")
    print(f"{len(keep)} points written to {args.equity_log} "
          f"({len(measured)} of them measured live)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
