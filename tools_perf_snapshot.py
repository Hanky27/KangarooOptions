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

"""Measure the competition account and write ONE json snapshot.

Every number in the snapshot comes from the broker through the same
Alpaca CLI the agent trades with - nothing is read from the agent's own
state files, so the snapshot cannot inherit a bookkeeping error from the
agent it is supposed to report on.

The one identity that ties the parts together is checked before anything
is written:

    equity - starting_equity  ==  realized + unrealized

The left side is the broker's own equity, the right side is rebuilt from
the fill list (realized, matched per contract in time order) and the
position list (unrealized). A mismatch beyond one cent means the
reconstruction is wrong and the run aborts instead of publishing a
plausible-looking number.

Usage:
    python tools_perf_snapshot.py --config config.yaml --out docs/snapshot.json
"""

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

import yaml

from alpaca_cli import AlpacaCli, AlpacaCliError

# OCC option symbol: root, YYMMDD, C/P, strike * 1000, zero padded to 8.
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

CONTRACT_MULTIPLIER = 100

# The account's cash is short of what the fills alone would leave by exactly
# 0.025 USD per contract traded, measured over 214 contracts (7,930.00 from
# the fills against 7,924.65 booked). That is the OCC clearing fee Alpaca
# documents at 0.025 per contract on buys and sells
# (https://alpaca.markets/support/what-are-the-fees-associated-with-options-trading).
# The rate is not assumed: the check below fails the run if the observed
# difference stops matching it, because then cash is moving for a reason
# this report does not model.
OCC_CLEARING_FEE = 0.025


def parse_occ(symbol: str) -> dict:
    m = OCC.match(symbol)
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol}")
    root, yymmdd, right, strike = m.groups()
    return {
        "underlying": root,
        "expiry": f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}",
        "right": right,
        "strike": int(strike) / 1000.0,
    }


def fetch_all_fills(cli: AlpacaCli) -> list[dict]:
    """Every FILL activity, oldest first.

    The endpoint pages; a caller that reads only the first page silently
    reports on part of the session. Paging stops when a page comes back
    shorter than the page size, and the last id of a page is the token
    for the next one.
    """
    page_size = 100
    rows: list[dict] = []
    token = None
    while True:
        args = ["account", "activity", "list",
                "--activity-types", "FILL",
                "--direction", "asc",
                "--page-size", str(page_size)]
        if token:
            args += ["--page-token", token]
        body = cli._run(args)
        page = body if isinstance(body, list) else (body or {}).get("activities", [])
        if not page:
            break
        rows += page
        if len(page) < page_size:
            break
        token = page[-1]["id"]
    return rows


def realized_by_contract(fills: list[dict]) -> tuple[float, dict[str, float], int, list[dict]]:
    """Match fills per contract in time order and return realized USD.

    Options here are opened by selling (short leg) and by buying (wing),
    so a contract's running position can be negative or positive. A fill
    that reduces the absolute position realizes against the average price
    of what is open; a fill that grows it moves that average. This is the
    average-cost rule, and it is exact for the whole-position case the
    grid produces.

    The fourth return value is the running total after every fill -
    {t, realized, fees} - which is what the balance line on the chart is
    drawn from. It comes out of THIS loop rather than a second one so the
    line and the headline figure can never drift apart; a second
    implementation of the same rule is exactly how the wing selection
    diverged from the simulator (see DEVLOG.md, ad109a5).
    """
    pos: dict[str, float] = collections.defaultdict(float)   # signed contracts
    avg: dict[str, float] = collections.defaultdict(float)   # average price
    realized: dict[str, float] = collections.defaultdict(float)
    closed_legs = 0
    running = 0.0
    fees = 0.0
    events: list[dict] = []

    def book(fill: dict, qty_abs: float) -> None:
        nonlocal fees
        fees += OCC_CLEARING_FEE * qty_abs
        events.append({
            "t": int(dt.datetime.fromisoformat(fill["transaction_time"]).timestamp()),
            "realized": running,
            "fees": fees,
        })

    for f in fills:
        sym = f["symbol"]
        qty = float(f["qty"]) * (1 if f["side"] == "buy" else -1)
        price = float(f["price"])
        held = pos[sym]

        if held == 0 or (held > 0) == (qty > 0):
            # opening or adding: move the average, realize nothing
            total = abs(held) + abs(qty)
            avg[sym] = (avg[sym] * abs(held) + price * abs(qty)) / total
            pos[sym] = held + qty
            book(f, abs(qty))
            continue

        # Reducing: realize over the overlapping quantity. A long leg earns
        # what it is sold above its average cost; a short leg earns what it
        # is bought back BELOW the average it was sold at, which is the same
        # expression with the sign of the position.
        closing = min(abs(qty), abs(held))
        direction = 1 if held > 0 else -1
        gain = direction * (price - avg[sym]) * closing * CONTRACT_MULTIPLIER
        realized[sym] += gain
        running += gain
        closed_legs += 1
        remainder = abs(qty) - closing
        pos[sym] = held + qty
        if remainder > 0:                          # flipped through zero
            avg[sym] = price
        elif pos[sym] == 0:
            avg[sym] = 0.0
        book(f, abs(qty))

    return sum(realized.values()), dict(realized), closed_legs, events


def group_positions(positions: list[dict]) -> list[dict]:
    """One row per instrument, an instrument being (underlying, direction).

    The grid's direction is readable from the contract right: this port
    sells put credit spreads on the long side and call credit spreads on
    the short side, so P means long and C means short.
    """
    rows: dict[tuple[str, str], dict] = {}
    for p in positions:
        info = parse_occ(p["symbol"])
        side = "long" if info["right"] == "P" else "short"
        key = (info["underlying"], side)
        row = rows.setdefault(key, {
            "underlying": info["underlying"],
            "direction": side,
            "contracts": 0,
            "unrealized": 0.0,
            "market_value": 0.0,
            "cost_basis": 0.0,
            "expiries": set(),
            "strikes": [],
        })
        row["contracts"] += 1
        row["unrealized"] += float(p["unrealized_pl"])
        row["market_value"] += float(p["market_value"])
        row["cost_basis"] += float(p["cost_basis"])
        row["expiries"].add(info["expiry"])
        row["strikes"].append(info["strike"])

    out = []
    for row in rows.values():
        row["expiries"] = sorted(row["expiries"])
        row["strikes"] = sorted(row["strikes"])
        # Two contracts make one spread: a short leg and its wing.
        row["spreads"] = row["contracts"] / 2.0
        # Credit still owed to close, i.e. what the legs are worth now.
        row["credit_open"] = -row["cost_basis"]
        out.append(row)
    return sorted(out, key=lambda r: r["unrealized"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--period", default="1D")
    ap.add_argument("--timeframe", default="5Min")
    ap.add_argument("--max-attempts", type=int, default=10,
                    help="how many times to re-sample when a fill lands "
                         "inside the reading window")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    cli = AlpacaCli(cfg["cli_path"], cfg["env_file"])

    clock = cli.clock()

    # The agent trades WHILE this runs, and cash, positions and fills are
    # three separate requests. A fill landing between them would be counted
    # on one side of the identity below and not the other, so the window is
    # bracketed by the fill list and repeated until no fill happened inside
    # it. This is a sampling loop, not a timeout: it ends on the observed
    # condition.
    for attempt in range(1, args.max_attempts + 1):
        fills = fetch_all_fills(cli)
        positions = cli.positions()
        account = cli.account()
        if len(fetch_all_fills(cli)) == len(fills):
            break
        print(f"a fill landed while sampling - retry {attempt}", file=sys.stderr)
    else:
        raise AlpacaCliError(
            f"no quiet window in {args.max_attempts} attempts - the account "
            f"is filling faster than it can be read consistently")

    history = cli._run(["account", "portfolio",
                        "--period", args.period,
                        "--timeframe", args.timeframe])

    start_equity = float(history["base_value"])
    cash = float(account["cash"])
    market_value = sum(float(p["market_value"]) for p in positions)
    unrealized = sum(float(p["unrealized_pl"]) for p in positions)
    realized, per_contract, closed_legs, events = realized_by_contract(fills)

    # Equity from the same numbers the report shows. The broker's own equity
    # field is marked at the instant IT was asked, a second or two away from
    # the position list, so the two differ by whatever the market did in
    # between; that drift is reported rather than hidden.
    equity = cash + market_value
    equity_broker = float(account["equity"])

    contracts_traded = sum(float(f["qty"]) for f in fills)
    fees = events[-1]["fees"] if events else 0.0

    # Balance in the sense the trading world reads it: what the account
    # would hold if every open spread vanished at zero - the start plus what
    # is booked. Equity is that plus the open marks, so the gap between the
    # two lines on the chart IS the unrealized number in the strip above.
    balance = start_equity + realized - fees

    # The identity, and the only part of it that can fail: everything else
    # cancels algebraically, so what is really tested here is that no cash
    # moved for a reason this report does not model - a fee at another rate,
    # an assignment, a transfer. Publishing a number that does not close is
    # worse than publishing nothing, so this is an abort, not a warning.
    residual = (equity - start_equity) - (realized + unrealized - fees)
    if abs(residual) > 0.01:
        raise AlpacaCliError(
            f"reconstruction does not close: equity change "
            f"{equity - start_equity:.2f} vs realized {realized:.2f} + "
            f"unrealized {unrealized:.2f} - fees {fees:.2f} "
            f"(residual {residual:.2f} over {contracts_traded:.0f} contracts, "
            f"{residual / contracts_traded:+.5f} per contract)")

    instruments = group_positions(positions)
    # Balance only moves when a fill books something, so it is a step
    # function sampled onto the equity bars by walking both sorted lists
    # once. Before the first fill it is the untouched start.
    curve = []
    cursor = 0
    for t, e, p in zip(history["timestamp"], history["equity"],
                       history["profit_loss"]):
        while cursor < len(events) and events[cursor]["t"] <= t:
            cursor += 1
        booked = events[cursor - 1] if cursor else {"realized": 0.0, "fees": 0.0}
        curve.append({
            "t": t,
            "equity": e,
            "pl": p,
            "balance": start_equity + booked["realized"] - booked["fees"],
        })
    # The history endpoint's last bar is the last CLOSED bar, so the curve
    # would stop minutes short of the equity in the headline and the
    # drawdown would be measured over a different window than the one shown.
    # The live point closes that gap, stamped with the BROKER's clock.
    now_ts = int(dt.datetime.fromisoformat(clock["timestamp"]).timestamp())
    if not curve or now_ts > curve[-1]["t"]:
        curve.append({"t": now_ts, "equity": equity, "pl": equity - start_equity,
                      "balance": balance})

    peak = start_equity
    max_dd = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_dd = min(max_dd, point["equity"] - peak)

    snapshot = {
        "account": account["account_number"],
        # Stamped from the broker's clock, not this machine's.
        "as_of": clock["timestamp"],
        "market_open": clock["is_open"],
        "start_equity": start_equity,
        "start_equity_asof": history["base_value_asof"],
        "equity": equity,
        "balance": balance,
        "equity_broker": equity_broker,
        "mark_drift": equity_broker - equity,
        "cash": cash,
        "market_value": market_value,
        "pl_total": equity - start_equity,
        "pl_pct": (equity - start_equity) / start_equity * 100.0,
        "realized": realized,
        "unrealized": unrealized,
        "fees": fees,
        "contracts_traded": contracts_traded,
        "residual": residual,
        "max_drawdown": max_dd,
        "initial_margin": float(account["initial_margin"]),
        "maintenance_margin": float(account["maintenance_margin"]),
        "open_contracts": len(positions),
        "open_spreads": len(positions) / 2.0,
        "instruments_open": len(instruments),
        "fills": len(fills),
        "closed_legs": closed_legs,
        "curve": curve,
        "instruments": instruments,
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1)

    print(f"equity {equity:,.2f}  P&L {equity - start_equity:+,.2f} "
          f"({snapshot['pl_pct']:+.2f}%)  realized {realized:+,.2f}  "
          f"unrealized {unrealized:+,.2f}  fees {fees:,.2f}  "
          f"residual {residual:+.4f}")
    print(f"{len(positions)} contracts in {len(instruments)} instruments, "
          f"{len(fills)} fills -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
