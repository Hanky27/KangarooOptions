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

# Fees are READ from the broker, never modelled. An earlier version of this
# file assumed 0.025 USD per contract - the OCC clearing rate, and correct
# for the first day, when that was the only booking on the account. It
# stopped being the whole fee the moment the regulatory charges settled
# after Monday's close: measured 2026-09-01, 15.12 USD booked against 7.75
# modelled, and the 7.37 difference aborted 20 consecutive publishes.
#
# The activity types this report knows how to place. Anything else is a
# cash movement it cannot classify, and it stops rather than fold an
# unknown booking into a number someone reads as trading performance.
FEE_TYPES = {"FEE"}
TRANSFER_TYPES = {"JNLC", "JNLS", "CSD", "CSW"}

# The equity curve is cross-checked against a second series and against the
# broker's own last_equity, because a curve that is wrong looks exactly
# like a curve that is right. The two readings never agree to the cent -
# they sample different bars - so the bar is set at 1 % of starting
# capital: measured spread between 5D/5Min and 5D/1H is at most 111 USD on
# 99,000 (0.11 %), while the defect this catches was 100,000 (100 %).
CURVE_TOLERANCE_PCT = 1.0


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
    {t, realized} - which is what the balance line on the chart is drawn
    from. It comes out of THIS loop rather than a second one so the line
    and the headline figure can never drift apart; a second
    implementation of the same rule is exactly how the wing selection
    diverged from the simulator (see DEVLOG.md, ad109a5).

    Fees are deliberately NOT here. They are the broker's bookings, not a
    consequence of the fills, and the version of this function that
    accrued them at an assumed rate is what put a 7.37 USD hole in the
    reconciliation (see fee_timeline below).
    """
    pos: dict[str, float] = collections.defaultdict(float)   # signed contracts
    avg: dict[str, float] = collections.defaultdict(float)   # average price
    realized: dict[str, float] = collections.defaultdict(float)
    closed_legs = 0
    running = 0.0
    events: list[dict] = []

    def book(fill: dict, qty_abs: float) -> None:
        events.append({
            "t": int(dt.datetime.fromisoformat(fill["transaction_time"]).timestamp()),
            "realized": running,
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


def split_activities(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate the broker's non-trade bookings into fees and transfers.

    Every row has to land in one bucket or the other. A booking this
    report cannot classify - a dividend, an assignment, a correction -
    changes the account by an amount that is not trading performance, and
    silently adding it to either side would misstate the result. So it
    stops, names the type, and leaves the previous snapshot standing.
    """
    fees = [r for r in rows if r.get("activity_type") in FEE_TYPES]
    transfers = [r for r in rows if r.get("activity_type") in TRANSFER_TYPES]
    unknown = {r.get("activity_type") for r in rows} - FEE_TYPES - TRANSFER_TYPES
    if unknown:
        raise AlpacaCliError(
            f"non-trade booking this report cannot classify: "
            f"{sorted(unknown)}. It moves the account without being a fee "
            f"or a transfer, so no number here would be honest until it is "
            f"modelled.")
    return fees, transfers


def fee_timeline(fees: list[dict]) -> list[dict]:
    """Cumulative fees booked, in the order the broker booked them.

    Sorted here rather than trusted from the CLI: `--direction asc` orders
    by the activity's own date and pages on an id that carries only a day
    stamp, so a day's rows arrive in uuid order (measured over 299 rows -
    first created_at 18:29:19Z, last 15:19:08Z). Returned as positive USD,
    the way the report speaks of a cost.
    """
    out: list[dict] = []
    running = 0.0
    for r in sorted(fees, key=lambda r: r["created_at"]):
        running += -float(r["net_amount"])
        out.append({
            "t": int(dt.datetime.fromisoformat(
                r["created_at"].replace("Z", "+00:00")).timestamp()),
            "fees": running,
        })
    return out


def fetch_curve(cli: AlpacaCli, account: dict) -> tuple[dict, dict]:
    """The equity history, checked against two independent readings.

    `--period 1D` was measured returning 200,000 for the open of a day the
    account started at 100,000, and drawing it produced a published max
    drawdown of -102,137.75 that never happened. Periods of 5D and longer
    read the same account correctly. Since a wrong curve is indis-
    tinguishable from a right one in the output, this checks the series it
    is about to draw:

      - against a second series of the SAME period at a different
        resolution, at every timestamp the two share;
      - against `last_equity`, the broker's own close of the previous
        trading day, at the curve point nearest that close.

    Points from before the account existed are dropped: the broker reports
    them as equity 0, and a zero in the curve is a 100 % drawdown in the
    drawdown line.
    """
    primary = cli._run(["account", "portfolio", "--period", "5D",
                        "--timeframe", "5Min"])
    control = cli._run(["account", "portfolio", "--period", "5D",
                        "--timeframe", "1H"])

    born = dt.datetime.fromisoformat(
        account["created_at"].replace("Z", "+00:00")).timestamp()
    keep = [i for i, t in enumerate(primary["timestamp"]) if t >= born]
    if not keep:
        raise AlpacaCliError(
            f"no history point after the account was created "
            f"({account['created_at']}) - nothing to draw")
    for key in ("timestamp", "equity", "profit_loss"):
        primary[key] = [primary[key][i] for i in keep]

    start_equity = float(primary["base_value"])
    limit = abs(start_equity) * CURVE_TOLERANCE_PCT / 100.0

    control_map = {t: float(e) for t, e in zip(control["timestamp"],
                                               control["equity"])}
    shared = [(t, float(e)) for t, e in zip(primary["timestamp"],
                                            primary["equity"])
              if t in control_map]
    if not shared:
        raise AlpacaCliError(
            "the two history series share no timestamp - the curve cannot "
            "be checked against a second reading")
    worst_t, worst = max(((t, abs(e - control_map[t])) for t, e in shared),
                         key=lambda pair: pair[1])
    if worst > limit:
        when = dt.datetime.fromtimestamp(
            worst_t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        raise AlpacaCliError(
            f"the 5Min and 1H histories disagree by {worst:,.2f} USD at "
            f"{when}Z, over the {limit:,.2f} allowed. One of them is not "
            f"this account - refusing to publish a curve on it.")

    drift = None
    i = _previous_close_index(primary["timestamp"])
    if i is not None:
        last_equity = float(account["last_equity"])
        drift = abs(float(primary["equity"][i]) - last_equity)
        if drift > limit:
            raise AlpacaCliError(
                f"the curve's previous close is "
                f"{float(primary['equity'][i]):,.2f} where the broker "
                f"reports last_equity {last_equity:,.2f} (off by "
                f"{drift:,.2f}, over the {limit:,.2f} allowed) - refusing "
                f"to publish a curve that is not this account.")

    return primary, {"curve_vs_control": worst,
                     "curve_vs_last_equity": drift,
                     "curve_tolerance": limit}


def _previous_close_index(timestamps: list[int]) -> int | None:
    """Index of the last point belonging to a day before the newest one.

    The check against `last_equity` only means something at the close of
    the PREVIOUS trading day; comparing today's live bar against
    yesterday's close would fail for the ordinary reason that the market
    moved. On the account's first day there is no earlier day, and None
    says so rather than inventing a comparison.
    """
    day = dt.datetime.fromtimestamp(timestamps[-1], dt.timezone.utc).date()
    for i in range(len(timestamps) - 1, -1, -1):
        if dt.datetime.fromtimestamp(timestamps[i], dt.timezone.utc).date() < day:
            return i
    return None


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

    history, curve_checks = fetch_curve(cli, account)

    # Everything that moved cash without a fill. The window starts before
    # the account existed, so nothing can fall outside it.
    bookings = cli.activities(after=account["created_at"][:10])
    fee_rows, transfer_rows = split_activities(bookings)

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

    # Fees as the broker booked them, not as a rate says they should be.
    fee_events = fee_timeline(fee_rows)
    fees = fee_events[-1]["fees"] if fee_events else 0.0

    # Money paid in before the first trade is starting capital and is
    # already inside start_equity; money moved afterwards is not trading
    # performance and has to appear in the identity or it would be read as
    # profit. The account's funding journal lands 7 h before the first fill,
    # which is why the report closes without special-casing it.
    first_fill = min((e["t"] for e in events), default=None)
    late_transfers = 0.0
    for r in transfer_rows:
        when = int(dt.datetime.fromisoformat(
            r["created_at"].replace("Z", "+00:00")).timestamp())
        if first_fill is not None and when > first_fill:
            late_transfers += float(r["net_amount"])

    # Balance in the sense the trading world reads it: what the account
    # would hold if every open spread vanished at zero - the start plus what
    # is booked. Equity is that plus the open marks, so the gap between the
    # two lines on the chart IS the unrealized number in the strip above.
    balance = start_equity + realized - fees

    # The identity, and the only part of it that can fail: everything else
    # cancels algebraically, so what is really tested here is that no cash
    # moved for a reason this report does not model - an assignment, a
    # correction, a fee the broker books under a type nobody has seen yet.
    # Publishing a number that does not close is worse than publishing
    # nothing, so this is an abort, not a warning.
    residual = ((equity - start_equity)
                - (realized + unrealized - fees + late_transfers))
    if abs(residual) > 0.01:
        raise AlpacaCliError(
            f"reconstruction does not close: equity change "
            f"{equity - start_equity:.2f} vs realized {realized:.2f} + "
            f"unrealized {unrealized:.2f} - fees {fees:.2f} "
            f"+ transfers {late_transfers:.2f} "
            f"(residual {residual:.2f} over {contracts_traded:.0f} contracts, "
            f"{residual / contracts_traded:+.5f} per contract)")

    instruments = group_positions(positions)
    # Balance only moves when a fill books something, so it is a step
    # function sampled onto the equity bars by walking both sorted lists
    # once. Before the first fill it is the untouched start.
    curve = []
    cursor = 0
    fee_cursor = 0
    for t, e, p in zip(history["timestamp"], history["equity"],
                       history["profit_loss"]):
        while cursor < len(events) and events[cursor]["t"] <= t:
            cursor += 1
        while (fee_cursor < len(fee_events)
               and fee_events[fee_cursor]["t"] <= t):
            fee_cursor += 1
        booked = events[cursor - 1]["realized"] if cursor else 0.0
        charged = fee_events[fee_cursor - 1]["fees"] if fee_cursor else 0.0
        curve.append({
            "t": t,
            "equity": e,
            "pl": p,
            "balance": start_equity + booked - charged,
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
        "fee_bookings": len(fee_rows),
        "transfers_after_first_fill": late_transfers,
        # What the curve was checked against before it was drawn, so a
        # reader can see the check happened and how much room was left.
        "curve_vs_control": curve_checks["curve_vs_control"],
        "curve_vs_last_equity": curve_checks["curve_vs_last_equity"],
        "curve_tolerance": curve_checks["curve_tolerance"],
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
          f"unrealized {unrealized:+,.2f}  fees {fees:,.2f} "
          f"({len(fee_rows)} bookings)  residual {residual:+.4f}")
    print(f"{len(positions)} contracts in {len(instruments)} instruments, "
          f"{len(fills)} fills -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
