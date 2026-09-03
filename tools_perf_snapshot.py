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

HERE = os.path.dirname(os.path.abspath(__file__))

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
# The rate cash is charged the instant a fill happens, before the broker
# books the settled fees that replace it. NOT the fee: it is only ever used
# to BOUND the gap between booked fees and what cash already reflects, and
# a gap outside that bound aborts the run instead of being explained away.
# Measured 2026-09-01: 182 contracts traded since the newest booked fee,
# identity short by exactly 4.55 = 182 x 0.025.
PROVISIONAL_FEE_RATE = 0.025

# How far the newest intraday bar may sit from the account's own equity
# before today's bars are refused. This is a LAG check, not a tolerance on
# truth: the newest bar is minutes old and the account is now. Measured
# 2026-09-01, the gap was 5.8 % and the bars were being revised by up to
# 961 USD between two reads of the same bar.
TODAY_BAR_TOLERANCE_PCT = 1.0

FEE_TYPES = {"FEE"}
TRANSFER_TYPES = {"JNLC", "JNLS", "CSD", "CSW"}

# What an option's last day looks like in the account, measured over all 32
# such rows on 2026-09-03:
#
#   OPEXP  expired          net_amount 0   qty +1 closing a short, -1 a long
#   OPASN  assigned         net_amount 0
#   OPEXC  exercised        net_amount 0
#   OPTRD  the stock leg    symbol GLD, qty 100, price 405, net -40500
#
# The first three move NO cash - they only take the option out of the
# account - so counting them as a fee or a transfer would be wrong in both
# directions. The cash is in OPTRD, which is a share trade at the strike.
#
# They still have to reach the books, because there is no closing FILL for
# any of them: an option sold for a credit and then assigned leaves that
# credit in cash with nothing on the other side of the identity. Each
# becomes a synthetic fill - see synthetic_fills for how the premium is
# placed, which is NOT the same for an assignment as for an exercise.
OPTION_SETTLEMENT_TYPES = {"OPEXP", "OPASN", "OPEXC"}
STOCK_TRADE_TYPES = {"OPTRD"}

# Shares are one for one; option contracts are a hundred.
STOCK_MULTIPLIER = 1

# Every timestamp out of the history endpoint is interpreted in market
# time, because a trading day is a market-hours window and the queries
# that fetch one are written in it.
ET = dt.timezone(dt.timedelta(hours=-4))


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


def synthetic_fills(rows: list[dict]) -> list[dict]:
    """The option lifecycle, expressed as the fills the account never got.

    There is no closing FILL when an option expires or is assigned - it
    simply leaves. Left out, the credit it was sold for sits in cash with
    nothing on the other side of the identity, and the report cannot close.

    Two shapes come out of here, both in the same dict shape fetch_all_fills
    returns so they can be merged into one time-ordered list:

    * the option, closed. `qty` in these rows carries the sign of the side
      being closed - measured over all 21 rows: +1 where a SHORT was taken
      away (IWM260901C00293000, the short call of the 293/298 spread) and
      -1 where a LONG was (IWM260901C00298000, its wing). So a positive qty
      is a buy-to-close and a negative one a sell-to-close. At WHAT price
      is the part that took two attempts to get right - see below.
    * the shares. OPTRD carries the underlying, the share count and the
      strike they changed hands at - GLD 100 at 405, AMZN -100 at 257.5 -
      and its own sign says which way: shares received are a buy, shares
      delivered a sell.

    An expiry worth nothing IS a close at zero, and one that stands alone
    (no OPTRD in its group) is exactly that.

    AN ASSIGNMENT IS NOT, AND AN EXERCISE IS NOT THE SAME AS AN ASSIGNMENT.
    The cash is identical in both - `net_amount` is the strike times the
    shares, exactly, in every one of the eleven rows. What differs is where
    the PREMIUM ends up, and the broker's own avg_entry_price says which:

        GLD  strike 405     basis 402.330   assigned  -> premium in basis
        SLV  strike 59.875  basis  59.125   assigned  -> premium in basis
        TLT  strike 82.5    basis  81.900   assigned  -> premium in basis
        AMZN strike 265     basis 265.000   exercised -> basis IS the strike

    A SHORT option taken away (OPASN) folds the premium you RECEIVED into
    the share basis and realizes nothing on the option. A LONG option
    exercised (OPEXC) realizes the premium you PAID as a loss and leaves
    the basis at the strike. Both readings of the account agree on which is
    which: every OPASN row carries a positive qty and every OPEXC a
    negative one, which is only what the words mean - you are assigned on
    what you are short, and your own longs are what get exercised.

    So, per group:

        short taken away   option closes at BOOK (realizes 0)
                           shares at strike + basis_sign * premium
        long exercised     option closes at ZERO (realizes -premium)
                           shares at the strike

    with basis_sign = -direction, direction being +1 when shares were
    received and -1 when delivered.

    Both halves have to move together. Closing at zero AND buying at the
    strike counts the premium twice - the identity came up 927.00 short, to
    the cent the sum of the three assigned differences above. Closing at
    book AND buying at the strike loses it entirely - tried, and it made
    the gap worse, 373.00 to 567.00. The pair above is the only combination
    that reproduces the broker's basis on every position it still holds.

    The option and its shares are paired by GROUP_ID, which the broker sets
    on exactly the two rows of one event (measured: 21 groups, every OPASN
    and OPEXC in a group of two with its OPTRD, every OPEXP alone). They
    are emitted at one timestamp with the option first, because the share
    price is read from the option's average and that average is gone once
    the position closes.
    """
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        kind = r.get("activity_type")
        if kind in OPTION_SETTLEMENT_TYPES or kind in STOCK_TRADE_TYPES:
            groups[r.get("group_id") or r["id"]].append(r)

    out: list[dict] = []
    for gid, rs in groups.items():
        opt = next((r for r in rs
                    if r["activity_type"] in OPTION_SETTLEMENT_TYPES), None)
        stock = next((r for r in rs
                      if r["activity_type"] in STOCK_TRADE_TYPES), None)
        if opt is None:
            raise AlpacaCliError(
                f"activity group {gid} has shares but no option event: "
                f"{[r['activity_type'] for r in rs]} - the share basis is "
                f"read from the option it came from, so this cannot be priced")
        when = min(r["created_at"] for r in rs)
        opt_qty = float(opt.get("qty") or 0)
        if not opt_qty:
            continue
        # Which side was taken away. Positive qty is a short being closed,
        # negative a long - and it decides both halves of the pair below.
        was_short = opt_qty > 0
        out.append({
            "symbol": opt["symbol"],
            "qty": abs(opt_qty),
            "side": "buy" if was_short else "sell",
            "price": 0.0,
            "transaction_time": when,
            "seq": 0,
            "multiplier": CONTRACT_MULTIPLIER,
            "synthetic": opt["activity_type"],
            # A short's premium goes into the share basis, so its option
            # closes at book and realizes nothing. A long's premium was
            # paid and is realized here, so that one closes at zero.
            "close_at_book": stock is not None and was_short,
        })
        if stock is None:
            continue
        sh_qty = float(stock.get("qty") or 0)
        direction = 1 if sh_qty > 0 else -1
        row = {
            "symbol": stock["symbol"],
            "qty": abs(sh_qty),
            "side": "buy" if sh_qty > 0 else "sell",
            "price": float(stock["price"]),
            "transaction_time": when,
            "seq": 1,
            "multiplier": STOCK_MULTIPLIER,
            "synthetic": stock["activity_type"],
        }
        if was_short:
            row["basis_from"] = opt["symbol"]
            row["basis_sign"] = -direction
        out.append(row)
    return out


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

    # An assigned option hands its premium to the shares it became. Both
    # sides need the SAME number, and it only exists while the option is
    # still open, so it is read here - one pass, one average, no second
    # implementation of the rule.
    premium: dict[str, float] = {}

    for f in fills:
        sym = f["symbol"]
        qty = float(f["qty"]) * (1 if f["side"] == "buy" else -1)
        price = float(f["price"])
        if f.get("close_at_book"):
            price = avg[sym]
            premium[sym] = avg[sym]
        elif f.get("basis_from"):
            src = f["basis_from"]
            if src not in premium:
                raise AlpacaCliError(
                    f"shares from {src} priced before the option was closed "
                    f"- the basis would be wrong and the identity would not "
                    f"close")
            price = price + f["basis_sign"] * premium[src]
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
        # Per fill, not a constant: the same loop now also matches SHARES,
        # which are one for one, against option contracts, which are a
        # hundred. An assignment puts both in the list.
        gain = (direction * (price - avg[sym]) * closing
                * float(f.get("multiplier", CONTRACT_MULTIPLIER)))
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

    book = {sym: (qty, avg[sym]) for sym, qty in pos.items() if qty}
    return sum(realized.values()), dict(realized), closed_legs, events, book


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
    # An option's last day is neither a fee nor a transfer - it reaches the
    # books as a synthetic fill instead (see synthetic_fills). The claim
    # that these three move no cash is CHECKED here rather than assumed: it
    # is the whole reason they may be left out of both buckets.
    for r in rows:
        if r.get("activity_type") in OPTION_SETTLEMENT_TYPES:
            moved = float(r.get("net_amount") or 0)
            if moved:
                raise AlpacaCliError(
                    f"{r['activity_type']} {r.get('symbol')} moved "
                    f"{moved} USD. These bookings are modelled as cashless "
                    f"position events, and one that moves cash breaks that "
                    f"- no number here would be honest until it is remodelled.")
    unknown = ({r.get("activity_type") for r in rows} - FEE_TYPES
               - TRANSFER_TYPES - OPTION_SETTLEMENT_TYPES - STOCK_TRADE_TYPES)
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


def daily_spine(cli: AlpacaCli, account: dict) -> list[tuple[int, float]]:
    """One point per closed day, checked against the broker's own figure.

    The daily series was the only query that read this account correctly
    in every measurement, including the ones where its baseline predated
    the account's funding. It is still not trusted on that record: its
    newest closed day has to equal `last_equity`, which is the same number
    arriving by a different route.

    Points before the account existed come back as equity 0. They are
    dropped: a zero in the curve is a 100 % drawdown in the drawdown line.
    """
    daily = cli._run(["account", "portfolio", "--period", "1M",
                      "--timeframe", "1D"])
    spine = [(int(t), float(e))
             for t, e in zip(daily["timestamp"], daily["equity"])
             if float(e) != 0.0]
    if not spine:
        raise AlpacaCliError(
            "the daily history holds no non-zero point - there is no "
            "verified series to hang the curve on")
    last_equity = float(account["last_equity"])
    drift = abs(spine[-1][1] - last_equity)
    if drift > 0.01:
        raise AlpacaCliError(
            f"the daily history's newest close is {spine[-1][1]:,.2f} where "
            f"the broker reports last_equity {last_equity:,.2f} (off by "
            f"{drift:,.2f}) - the spine is not this account, so nothing "
            f"built on it can be published")
    return spine


def intraday(cli: AlpacaCli, day: str,
             clock: dict | None = None) -> list[tuple[int, float]]:
    """One trading day at 5-minute resolution, or nothing.

    A session that has not started yet has no bars, and asking for one is
    an ERROR rather than an empty answer: the endpoint clamps `end` to the
    last session it has and then rejects its own window - measured
    2026-09-03 at 05:53 ET,

      "start cannot be after end. start: 2026-09-03T09:30:00-04:00,
       end: 2026-09-02T09:30:00-04:00: bad request"

    which took the whole publish down every five minutes between midnight
    and the opening bell. The clock says whether the day has begun: while
    the market is closed and the next open is still today, it has not.
    """
    if clock and not clock.get("is_open"):
        nxt = str(clock.get("next_open") or "")
        if nxt[:10] == day and clock["timestamp"] < nxt:
            return []
    h = cli._run(["account", "portfolio",
                  "--start", f"{day}T09:30:00-04:00",
                  "--end", f"{day}T16:00:00-04:00",
                  "--timeframe", "5Min"])
    return [(int(t), float(e))
            for t, e in zip(h.get("timestamp") or [], h.get("equity") or [])]


def fetch_curve(cli: AlpacaCli, account: dict,
                clock: dict) -> tuple[list[tuple[int, float]], dict]:
    """The equity series, assembled from parts that each had to check out.

    Returns the points and a record of what was accepted and what was
    refused, so the page can show that the check happened rather than
    merely claiming it.
    """
    spine = daily_spine(cli, account)
    today = clock["timestamp"][:10]
    checks = {"spine_days": len(spine), "intraday_days": [],
              "rejected_days": []}

    points: list[tuple[int, float]] = []
    for ts, close in spine:
        day = dt.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")
        bars = intraday(cli, day, clock)
        # The day's own bars are kept only if they end where the spine
        # says that day ended. Monday - the funding day - does not, and
        # contributes its single close instead of a line 100,000 too high.
        if bars and abs(bars[-1][1] - close) <= 0.01:
            points += bars
            checks["intraday_days"].append(day)
        else:
            points.append((ts, close))
            if bars:
                checks["rejected_days"].append(
                    {"day": day, "intraday_close": round(bars[-1][1], 2),
                     "daily_close": round(close, 2)})

    # Today has no close to check against, so it gets two checks instead.
    # Its baseline must be yesterday's close - the number already verified
    # above - and its newest bar must be within lag distance of the account
    # as it stands right now.
    if not any(dt.datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d") == today
               for t, _e in points):
        bars = intraday(cli, today, clock)
        h = cli._run(["account", "portfolio", "--period", "1D",
                      "--timeframe", "5Min"])
        equity_now = float(account["equity"])
        base_gap = abs(float(h["base_value"]) - float(account["last_equity"]))
        limit = abs(equity_now) * TODAY_BAR_TOLERANCE_PCT / 100.0
        bar_gap = abs(bars[-1][1] - equity_now) if bars else None
        if bars and base_gap <= 0.01 and bar_gap is not None \
                and bar_gap <= limit:
            points += bars
            checks["intraday_days"].append(today)
        elif bars:
            checks["rejected_days"].append(
                {"day": today,
                 "newest_bar": round(bars[-1][1], 2),
                 "account_equity": round(equity_now, 2),
                 "gap": round(bar_gap, 2) if bar_gap is not None else None,
                 "allowed": round(limit, 2),
                 "base_gap": round(base_gap, 2)})

    points.sort()
    if not points:
        raise AlpacaCliError("no history point survived the checks")
    return points, checks


def read_equity_log(path: str) -> list[dict]:
    """Every point this report has measured before, oldest first."""
    rows: list[dict] = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["t"])
    return rows


def append_equity_point(path: str, point: dict) -> None:
    """Add one measured point, unless this second is already recorded.

    Append-only on purpose: a point that has been published is a thing
    that was true at a moment, and rewriting history is the one thing this
    report exists to make impossible.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = read_equity_log(path)
    if existing and existing[-1]["t"] >= point["t"]:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(point, sort_keys=True) + "\n")


def funding(transfers: list[dict], first_fill: int | None) -> float:
    """Starting capital, read from the account's own cash bookings.

    Not from `base_value`: that is whichever baseline the history endpoint
    picked, and it was measured returning a date on which this account did
    not exist. Money paid in BEFORE the first fill is the capital the
    result is measured against; money moved afterwards is not.
    """
    total = 0.0
    for r in transfers:
        when = int(dt.datetime.fromisoformat(
            r["created_at"].replace("Z", "+00:00")).timestamp())
        if first_fill is None or when <= first_fill:
            total += float(r["net_amount"])
    return total


def group_positions(positions: list[dict]) -> list[dict]:
    """One row per instrument, an instrument being (underlying, direction).

    The grid's direction is readable from the contract right: this port
    sells put credit spreads on the long side and call credit spreads on
    the short side, so P means long and C means short.

    SHARES get their own rows. They are not part of any spread - they are
    what an assignment left behind, and the agent flattens them on the next
    open poll - so counting them as contracts would put half a spread into
    a grid that does not hold one. They carry `assigned`, and the page can
    say what they are instead of pretending they are a leg.
    """
    rows: dict[tuple[str, str], dict] = {}
    for p in positions:
        if OCC.match(p["symbol"]) is None:
            qty = float(p["qty"])
            rows[(p["symbol"], "assigned")] = {
                "underlying": p["symbol"],
                "direction": "assigned",
                "assigned": True,
                "shares": qty,
                "contracts": 0,
                "unrealized": float(p["unrealized_pl"]),
                "market_value": float(p["market_value"]),
                "cost_basis": float(p["cost_basis"]),
                "avg_entry": float(p["avg_entry_price"]),
                "expiries": set(),
                "strikes": [],
            }
            continue
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
        # Shares hold no credit - their cost_basis is what was paid for
        # them, and calling that a credit would add an assignment's whole
        # notional to the total the page shows as premium collected.
        row["credit_open"] = 0.0 if row.get("assigned") else -row["cost_basis"]
        out.append(row)
    return sorted(out, key=lambda r: r["unrealized"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--equity-log", default="state/equity_log.jsonl",
                    help="append-only record of every equity this report "
                         "has measured; the curve is drawn from it. A "
                         "relative path is resolved against this file's "
                         "directory, never the working directory")
    ap.add_argument("--max-attempts", type=int, default=10,
                    help="how many times to re-sample when a fill lands "
                         "inside the reading window")
    args = ap.parse_args()

    # ABSOLUTE, against this file's directory - never the working
    # directory. The publish job is a scheduled task with an EMPTY
    # WorkingDirectory, so Windows starts it in C:\Windows\System32, where
    # the default "state/equity_log.jsonl" became a request to create a
    # `state` folder inside System32. Measured 2026-09-03: every scheduled
    # run died with "PermissionError: [WinError 5] Zugriff verweigert:
    # 'state'" while the same command run by hand in the repo worked, which
    # is the worst shape a bug can have. Same rule the agent already
    # applies to its per-instrument state files.
    if not os.path.isabs(args.equity_log):
        args.equity_log = os.path.join(HERE, args.equity_log)

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

    # Everything that moved cash without a fill. The window starts before
    # the account existed, so nothing can fall outside it.
    bookings = cli.activities(after=account["created_at"][:10])
    fee_rows, transfer_rows = split_activities(bookings)

    # An option that expired or was assigned never produced a closing FILL,
    # and the shares it turned into never produced an opening one. Both are
    # merged into the fill list, in time order, so ONE average-cost pass
    # sees the whole life of every position.
    fills = sorted(fills + synthetic_fills(bookings),
                   key=lambda f: (f["transaction_time"], f.get("seq", 0)))

    cash = float(account["cash"])
    market_value = sum(float(p["market_value"]) for p in positions)
    realized, per_contract, closed_legs, events, book = (
        realized_by_contract(fills))
    # Unrealized against the basis THIS report rebuilt, not the broker's
    # unrealized_pl field. The two halves of the identity have to be
    # measured against the same cost, and on 2026-09-03 the broker's was
    # not: SLV's avg_entry_price 59.1250 folds 600.00 of premium into 800
    # shares where the six assignment orders received 597.00 - verified
    # against the fills AND the orders, 0.71+0.69+0.79+0.84+2x0.84 and
    # 2x0.63. Ours is the cash that actually moved, so ours is the basis
    # the identity is measured on, and the difference is REPORTED rather
    # than absorbed: it had been landing in the provisional fee term,
    # which is bounded and would have hidden it.
    unrealized = 0.0
    basis_drift: list[dict] = []
    for p in positions:
        sym = p["symbol"]
        qty, our_avg = book.get(sym, (0.0, 0.0))
        mult = STOCK_MULTIPLIER if OCC.match(sym) is None else CONTRACT_MULTIPLIER
        if abs(qty - float(p["qty"])) > 1e-9:
            raise AlpacaCliError(
                f"{sym}: the rebuilt books hold {qty:g} where the account "
                f"holds {p['qty']} - the reconstruction is missing a "
                f"transaction, and no number built on it would be honest")
        unrealized += float(p["market_value"]) - qty * our_avg * mult
        drift = (our_avg - float(p["avg_entry_price"])) * abs(qty) * mult
        if abs(drift) >= 0.005:
            basis_drift.append({
                "symbol": sym, "ours": round(our_avg, 4),
                "broker": round(float(p["avg_entry_price"]), 4),
                "usd": round(drift, 2)})

    # Starting capital is what was paid IN, read from the account's own
    # cash bookings - not from the history endpoint's base_value, which was
    # measured naming a date on which this account did not exist.
    first_fill = min((e["t"] for e in events), default=None)
    start_equity = funding(transfer_rows, first_fill)
    if start_equity <= 0:
        raise AlpacaCliError(
            f"no funding found before the first fill - starting capital "
            f"would be {start_equity}, and every figure here is measured "
            f"against it")
    curve_points, curve_checks = fetch_curve(cli, account, clock)

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

    # The mirror of `funding`: what was paid in AFTER trading began is not
    # performance, so it has to appear in the identity or it would be read
    # as profit. The account's funding journal lands 7 h before the first
    # fill, which is why the report closes without special-casing it.
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
    unexplained = ((equity - start_equity)
                   - (realized + unrealized - fees + late_transfers))

    # Fees charged to cash that the broker has not booked yet. Bounded by
    # the provisional rate on the contracts traded since the newest booking
    # - anything beyond that is cash moving for a reason this report does
    # not model, and is still an abort.
    newest_booking = max((e["t"] for e in fee_events), default=0)
    unbooked_contracts = sum(float(f["qty"]) for f in fills
                             if int(dt.datetime.fromisoformat(
                                 f["transaction_time"]).timestamp())
                             > newest_booking)
    bound = PROVISIONAL_FEE_RATE * unbooked_contracts
    fees_provisional = -unexplained
    residual = unexplained + fees_provisional
    if not (-0.01 <= fees_provisional <= bound + 0.01):
        raise AlpacaCliError(
            f"reconstruction does not close: equity change "
            f"{equity - start_equity:.2f} vs realized {realized:.2f} + "
            f"unrealized {unrealized:.2f} - fees {fees:.2f} "
            f"+ transfers {late_transfers:.2f} leaves {unexplained:.2f} "
            f"unexplained, outside the 0.00..{bound:.2f} that "
            f"{unbooked_contracts:.0f} contracts could have accrued at "
            f"{PROVISIONAL_FEE_RATE} since the newest booked fee")

    # The curve is this report's OWN measurements from here on. The
    # broker's intraday series was measured 5,274.55 away from the account
    # at the same instant, so it is not the quantity this page reports;
    # the verified daily closes still seed the days before recording began.
    now_ts = int(dt.datetime.fromisoformat(clock["timestamp"]).timestamp())
    append_equity_point(args.equity_log, {
        "t": now_ts,
        "equity": round(equity, 2),
        "balance": round(balance, 2),
        "realized": round(realized, 2),
        "fees": round(fees + fees_provisional, 2),
    })
    recorded = read_equity_log(args.equity_log)
    earliest_recorded = recorded[0]["t"] if recorded else now_ts
    # Only the days between the first fill and the start of recording are
    # worth seeding. The 79 bars of the Friday before, all at exactly
    # 100,000 because nothing had traded yet, said nothing and squashed
    # everything that did into the right-hand edge.
    seeded = [(t, e) for t, e in curve_points
              if first_fill is not None and first_fill <= t < earliest_recorded]
    if first_fill is not None and first_fill < earliest_recorded:
        seeded.insert(0, (first_fill, start_equity))

    instruments = group_positions(positions)
    # Balance only moves when a fill books something, so it is a step
    # function sampled onto the equity bars by walking both sorted lists
    # once. Before the first fill it is the untouched start.
    curve = []
    cursor = 0
    fee_cursor = 0
    for t, e in seeded:
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
            # P&L against the capital actually paid in, so the line and the
            # headline are the same subtraction.
            "pl": e - start_equity,
            "balance": start_equity + booked - charged,
        })
    # The balance moves only when a fill books something, and every fill
    # is timestamped, so this line is exact for every minute of the week
    # without asking any endpoint for it. equity is null here: it is not
    # known between two measurements and will not be invented.
    for e in events:
        while fee_cursor < len(fee_events) and fee_events[fee_cursor]["t"] <= e["t"]:
            fee_cursor += 1
        charged = fee_events[fee_cursor - 1]["fees"] if fee_cursor else 0.0
        curve.append({"t": e["t"], "equity": None, "pl": None,
                      "balance": start_equity + e["realized"] - charged})

    # Everything this report has measured itself, each point exactly as it
    # was published at the time. No interpolation, no re-marking: the last
    # entry IS the headline of this run.
    for row in recorded:
        curve.append({"t": row["t"],
                      "equity": row["equity"],
                      "pl": row["equity"] - start_equity,
                      "balance": row["balance"]})
    curve.sort(key=lambda point: point["t"])

    peak = start_equity
    max_dd = 0.0
    for point in curve:
        if point["equity"] is None:
            continue
        peak = max(peak, point["equity"])
        max_dd = min(max_dd, point["equity"] - peak)

    snapshot = {
        "account": account["account_number"],
        # Stamped from the broker's clock, not this machine's.
        "as_of": clock["timestamp"],
        "market_open": clock["is_open"],
        "start_equity": start_equity,
        "start_equity_asof": min(
            (r["created_at"][:10] for r in transfer_rows), default=None),
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
        "fees_provisional": fees_provisional,
        "fees_provisional_bound": bound,
        "fee_bookings": len(fee_rows),
        # Where the broker's own avg_entry_price disagrees with the cost
        # this report rebuilt from the cash that moved. Published rather
        # than reconciled away: on 2026-09-03 it is SLV, 3.00, because the
        # broker folded 600.00 of premium into 800 assigned shares where
        # the six orders that sold those puts received 597.00.
        "basis_drift": basis_drift,
        "basis_drift_usd": round(sum(d["usd"] for d in basis_drift), 2),
        "transfers_after_first_fill": late_transfers,
        # What the curve was assembled from, so a reader can see which
        # days were verified and which were refused rather than trusting
        # that a check happened.
        "curve_recorded_points": len(recorded),
        "curve_seeded_points": len(seeded),
        "curve_spine_days": curve_checks["spine_days"],
        "curve_intraday_days": curve_checks["intraday_days"],
        "curve_rejected_days": curve_checks["rejected_days"],
        "contracts_traded": contracts_traded,
        "residual": residual,
        "max_drawdown": max_dd,
        "initial_margin": float(account["initial_margin"]),
        "maintenance_margin": float(account["maintenance_margin"]),
        "open_contracts": len(positions),
        "open_spreads": len(positions) / 2.0,
        # GRIDS, not table rows: the assigned-share rows are what an
        # exercise left behind, not an instrument the agent runs.
        "instruments_open": sum(1 for r in instruments
                                if not r.get("assigned")),
        "assigned_rows": sum(1 for r in instruments if r.get("assigned")),
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
          f"({len(fee_rows)} booked + {fees_provisional:,.2f} provisional "
          f"of max {bound:,.2f})  residual {residual:+.4f}")
    print(f"{len(positions)} contracts in {len(instruments)} instruments, "
          f"{len(fills)} fills -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
