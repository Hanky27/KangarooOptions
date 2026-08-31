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

Simulation of the Kangaroo grid on SPY at daily OR hourly resolution
(--timeframe 1Day/1Hour; [HOURLY 29.08.2026] - hourly runs key every mark by
the bar's full UTC timestamp and settle expiries on the last bar of the
expiry day), in two styles:

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
  - The take-profit check runs on the WHOLE cluster: open legs plus the
    sunk pot of already-settled (expired) legs, mirroring the original's
    ClusterProfit = open positions + mClosedProfit (Instance.cs:442,
    tested at :618). The threshold is positive, so a cluster never takes
    profit while its total is negative - a loss on settled legs has to be
    earned back first. A cluster whose legs ALL expire still books that
    pot; there is nothing left to hold on to.
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
import time
from datetime import date, timedelta

from kangaroo_core import KangarooCore, settlement_pnl
from backtest_underlying import RANGE_BLOWOUT_PCT, KNOWN_DEFECT_DAYS

DEFAULT_PARAMS = dict(rebuy_1st_pct=1.2, rebuy_pct=0.10, tp_pct=0.10,
                      max_adverse_pct=0.0,
                      initial_qty=1, max_invest_count=20, start_long=True)

CLI_PATH = "C:/Users/HMz/Documents/Source/AlpacaTools/cli/alpaca.exe"
ENV_FILE = "C:/Users/HMz/Documents/Source/McpServer/alpaca-mcp-server/dist/env.txt"
# [HOURLY 29.08.2026] One cache dir per bar resolution - a 1Day close keyed by
# date and a 1Hour close keyed by full timestamp must never share a file.
CACHE_DIRS = {"1Day": "data/option_bars", "1Hour": "data/option_bars_1h"}

RATE_LIMIT_RETRIES = 8       # a throttle clears; a real error does not
RATE_LIMIT_BASE_WAIT_S = 5   # waited 5s, 10s, 15s ... between attempts

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


def fetch_option_closes(occ: str, start: str, end: str,
                        timeframe: str = "1Day") -> dict[str, float]:
    """Close per bar stamp for one OCC contract, cached on disk.

    Key format follows the resolution: 1Day -> "YYYY-MM-DD" (unchanged),
    1Hour -> the bar's full UTC timestamp as Alpaca returns it
    ("YYYY-MM-DDTHH:00:00Z"). start/end stay DATE strings either way."""
    global _ENV
    cache_dir = CACHE_DIRS[timeframe]
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{occ}.json")
    # The cache key is the CONTRACT, not the query range, so a file written
    # for one entry day does not necessarily cover another. A hit that
    # starts after the requested start OR ends before the requested end is
    # too short: the entry stamp would be missing, pick_spread would reject
    # a contract that does exist, and the run would take a different path.
    # BOTH edges have been measured to matter:
    #   left  - 2026-08-30, the 2026 long run: 2 of 274 hits started late,
    #           enough to move the result by a few hundred USD between runs
    #           with different cache states.
    #   right - 2026-08-31, the two-week search: SPY260831C00769000 was
    #           cached 08-20..08-27 and answered a request for 08-28 with
    #           that stale range, so the 08-28T19:00 entry stamp was
    #           missing and the run aborted with "no tradable call credit
    #           spread on 2026-08-28T19:00:00Z" - while the contract has 47
    #           hourly bars on that very day.
    # Such a hit is refetched over the union and merged, so the file only
    # ever grows. The union end is capped at `end`, which the caller
    # already clamps to the last day with underlying data.
    cached = None
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
        if not cached:
            return cached
        have_lo = min(k[:10] for k in cached)
        have_hi = max(k[:10] for k in cached)
        if start >= have_lo and end <= have_hi:
            return cached
        print(f"cache too short for {occ}: has {have_lo}..{have_hi}, "
              f"need {start}..{end} - refetching", flush=True)
        start, end = min(start, have_lo), max(end, have_hi)
    if _ENV is None:
        _ENV = _cli_env()
    args = [CLI_PATH, "data", "option", "bars", "--symbols", occ,
            "--timeframe", timeframe, "--start", start, "--end", end,
            "--limit", "1000", "-q"]
    # HTTP 429 is a throttle, not an answer: the server is telling us to
    # slow down, and the contract's bars are still there. Waiting is the
    # correct response - aborting the run would turn "you are too fast"
    # into a missing-data error. Anything else still fails loud, and so
    # does a throttle that will not clear.
    for attempt in range(RATE_LIMIT_RETRIES):
        proc = subprocess.run(args, capture_output=True, text=True, env=_ENV)
        combined = f"{proc.stdout} {proc.stderr}"
        throttled = ('"status": 429' in combined
                     or "too many requests" in combined.lower())
        if not throttled:
            break
        wait = RATE_LIMIT_BASE_WAIT_S * (attempt + 1)
        print(f"rate limited on {occ}, waiting {wait}s "
              f"(attempt {attempt + 1}/{RATE_LIMIT_RETRIES})", flush=True)
        time.sleep(wait)
    else:
        raise RuntimeError(
            f"option bars {occ}: still rate limited after "
            f"{RATE_LIMIT_RETRIES} attempts - reduce the number of "
            f"concurrent runs")
    if proc.returncode != 0:
        raise RuntimeError(f"option bars {occ} failed: "
                           f"{proc.stdout.strip()} {proc.stderr.strip()}")
    body = json.loads(proc.stdout)
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(f"option bars {occ}: {body['error']}")
    bars = (body.get("bars") or {}).get(occ) or []
    if timeframe == "1Day":
        closes = {b["t"][:10]: b["c"] for b in bars}
    else:
        closes = {b["t"]: b["c"] for b in bars}
    if cached:
        merged = dict(cached)
        merged.update(closes)
        closes = merged
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(closes, fh)
    os.replace(tmp, path)
    return closes


CHAIN_DIR = "data/chains"


def fetch_chain(underlying: str, right: str, exp_from: str,
                exp_to: str) -> dict[str, list[tuple[float, str]]]:
    """Real contracts per expiry: {"YYYY-MM-DD": [(strike, occ), ...]}.

    Asks Alpaca which contracts EXIST instead of building OCC symbols from
    a guessed expiry and a guessed strike grid. Measured 2026-08-30 over
    five sample days and the DTE window 4-10: of the 25 expiry dates the
    old "entry day + N weekdays" rule produced, 22 exist for SPY (88 %) but
    only 3 for DIA (12 %), 11 for AAPL (44 %) and 13 for GLD (52 %). At
    three of the five DIA sample days the whole window contained no expiry
    at all. The strike grid differs as well - AAPL and NVDA trade in 2.50
    steps, SPY and DIA in whole dollars - so of the five strikes the old
    rule probed around the spot, all five exist for SPY and DIA but only
    one for AAPL and NVDA. Those two guesses together are what made several
    underlyings look like they had no option data.

    This is also what the live agent does (agent.py select_spread ->
    cli.option_contracts), so simulator and bot now pick from one universe.

    Both contract states are queried because an expiry in the past is
    `inactive`. Cached per (underlying, right, range).
    """
    global _ENV
    os.makedirs(CHAIN_DIR, exist_ok=True)
    path = os.path.join(
        CHAIN_DIR, f"{underlying}_{right}_{exp_from}_{exp_to}.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return {k: [tuple(x) for x in v]
                    for k, v in json.load(fh).items()}
    if _ENV is None:
        _ENV = _cli_env()
    by_exp: dict[str, list[tuple[float, str]]] = {}
    for status in ("inactive", "active"):
        args = [CLI_PATH, "option", "contracts",
                "--underlying-symbols", underlying,
                "--expiration-date-gte", exp_from,
                "--expiration-date-lte", exp_to,
                "--type", right, "--status", status,
                "--limit", "10000", "-q"]
        proc = subprocess.run(args, capture_output=True, text=True, env=_ENV)
        if proc.returncode != 0:
            raise RuntimeError(
                f"option contracts {underlying} {exp_from}..{exp_to} "
                f"({status}) failed: {proc.stdout.strip()} "
                f"{proc.stderr.strip()}")
        body = json.loads(proc.stdout)
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(
                f"option contracts {underlying}: {body['error']}")
        rows = body.get("option_contracts") or []
        if len(rows) >= 10000:
            raise RuntimeError(
                f"option contracts {underlying} {exp_from}..{exp_to} hit the "
                f"10000 row cap - the list would be truncated and silently "
                f"hide expiries; narrow the range")
        for c in rows:
            by_exp.setdefault(c["expiration_date"], []).append(
                (float(c["strike_price"]), c["symbol"]))
    for v in by_exp.values():
        v.sort()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(by_exp, fh)
    os.replace(tmp, path)
    return by_exp


def occ_symbol(underlying: str, expiry: date, right: str, strike: int) -> str:
    return (f"{underlying}{expiry.strftime('%y%m%d')}"
            f"{'C' if right == 'call' else 'P'}{strike * 1000:08d}")


def pick_contract(underlying: str, entry_stamp: str, right: str, close: float,
                  dte_min: int, dte_max: int, last_day: str,
                  timeframe: str = "1Day") -> tuple[str, date, float] | None:
    """Nearest REAL expiration in the DTE window, strike nearest to the
    close - both taken from the exchange's contract list (fetch_chain).
    Returns (occ, expiry, entry_close) of the first candidate that traded
    on the entry bar. entry_stamp is the bar key ("YYYY-MM-DD" daily, full
    timestamp hourly); CLI fetch windows use its DATE part and are capped
    at last_day, because Alpaca rejects a request whose end includes the
    CURRENT day with 403 'OPRA agreement is not signed' (measured
    2026-08-28)."""
    entry_day = entry_stamp[:10]
    d0 = date.fromisoformat(entry_day)
    chain = fetch_chain(underlying, right,
                        (d0 + timedelta(days=dte_min)).isoformat(),
                        (d0 + timedelta(days=dte_max)).isoformat())
    for exp in sorted(chain):
        for strike, occ in sorted(chain[exp],
                                  key=lambda x: abs(x[0] - close))[:5]:
            closes = fetch_option_closes(
                occ, entry_day, min(exp, last_day), timeframe)
            if entry_stamp in closes:
                return occ, date.fromisoformat(exp), closes[entry_stamp]
    return None


def pick_spread(underlying: str, entry_stamp: str, right: str, close: float,
                dte_min: int, dte_max: int, last_day: str,
                timeframe: str = "1Day", widths=None):
    """Credit spread: ATM short leg + protective wing (above the short
    strike for calls, below for puts), both chosen from the REAL contract
    list.

    `widths` overrides SPREAD_WIDTHS with price-scaled candidates - a fixed
    5 USD wing is 0.71 % of SPY but 4.7 % of TLT, i.e. a different trade.
    The wing is the existing strike NEAREST to the requested distance, so a
    symbol whose grid is 2.50 or 5 USD wide is served instead of skipped.

    Iterates real expiries and wing candidates; a missing wing at one
    expiry moves on to the next. Returns (occ_s, strike_s, closes_s, occ_w,
    strike_w, closes_w, expiry) of the first candidate whose both legs
    traded on the entry bar with a positive net credit, or None.
    """
    entry_day = entry_stamp[:10]
    d0 = date.fromisoformat(entry_day)
    sign = 1 if right == "call" else -1
    widths = SPREAD_WIDTHS if widths is None else widths
    chain = fetch_chain(underlying, right,
                        (d0 + timedelta(days=dte_min)).isoformat(),
                        (d0 + timedelta(days=dte_max)).isoformat())
    for exp in sorted(chain):
        by_strike = dict(chain[exp])
        for strike_s, occ_s in sorted(chain[exp],
                                      key=lambda x: abs(x[0] - close))[:5]:
            closes_s = fetch_option_closes(
                occ_s, entry_day, min(exp, last_day), timeframe)
            if entry_stamp not in closes_s:
                continue
            wing_side = [k for k in by_strike
                         if (k > strike_s if sign > 0 else k < strike_s)]
            if not wing_side:
                continue
            seen = set()
            for width in widths:
                want = strike_s + sign * width
                strike_w = min(wing_side, key=lambda k: abs(k - want))
                if strike_w in seen:
                    continue
                seen.add(strike_w)
                occ_w = by_strike[strike_w]
                closes_w = fetch_option_closes(
                    occ_w, entry_day, min(exp, last_day), timeframe)
                if (entry_stamp in closes_w
                        and closes_s[entry_stamp] - closes_w[entry_stamp] > 0):
                    return (occ_s, strike_s, closes_s,
                            occ_w, strike_w, closes_w,
                            date.fromisoformat(exp))
    return None


# --- simulation ----------------------------------------------------------

def run(bars: list[dict], params: dict, dte_min: int, dte_max: int,
        spread_width_pct: float = 0.0,
        underlying: str = "SPY", sma: dict[str, float] | None = None,
        style: str = "long_options", regime: str = "mode1",
        cost_usd: float = 0.0, timeframe: str = "1Day",
        on_day=None, on_cluster=None) -> dict:
    """cost_usd: execution cost in USD per spread/contract per EXECUTION -
    charged once at every leg open and once at every TP close; an expired
    leg has no closing trade and therefore no exit cost. The take-profit
    TRIGGER stays cost-free (the original grid knows no costs); the costs
    reduce the booked cluster results.

    timeframe [HOURLY 29.08.2026]: "1Day" (unchanged behaviour, bar key =
    date) or "1Hour" (bar key = full UTC timestamp; marks, entries and TP
    checks run per hour bar; expiry settles on the LAST bar of the expiry
    day, which on 1Day is the day bar itself - one code path, REGEL 0.6/0.12).

    on_day(index, total, stamp, realized_total, open_mark): read-only
    observer called once per processed bar (drives the GUI equity stream).
    on_cluster(cluster_dict): read-only observer called at every cluster
    end with the row just appended to the result."""
    core = KangarooCore(**params)
    mult = core.contract_multiplier
    last_day = bars[-1]["t"][:10]
    daily = timeframe == "1Day"

    leg_meta: dict[str, dict] = {}
    cluster_start = None
    # Underlying close when the cluster's first leg opened. Reported per
    # cluster so the GUI can place its trade markers on a real level of the
    # underlying axis - an option premium (or a 0) on that axis drags the
    # chart's y-fit to meaningless bounds.
    cluster_start_price = None
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
        """Settle one expiring leg through the shared core formula and keep
        the ITM/OTM tally here (the core stays free of run statistics)."""
        meta = leg_meta[leg.option_symbol]
        pnl, itm = settlement_pnl(
            meta["kind"], meta.get("right", ""), meta["strike"],
            meta.get("wing_strike"), leg.entry_premium, leg.qty, close, mult)
        stats["legs_expired_itm" if itm else "legs_expired_otm"] += 1
        return pnl

    def leg_margin(leg) -> float:
        meta = leg_meta[leg.option_symbol]
        if meta["kind"] == "short_put":
            return meta["strike"] * mult * leg.qty
        if meta["kind"].endswith("_spread"):
            return abs(meta["wing_strike"] - meta["strike"]) * mult * leg.qty
        return 0.0

    def end_cluster(kind: str, stamp: str, result: float,
                    legs_at_end: int, close_price: float) -> None:
        nonlocal cluster_start, cluster_start_price, realized_total
        realized_total += result
        clusters.append({
            "cluster_id": core.cluster_id,
            "direction": "long" if core.is_long else "short",
            "start": cluster_start, "end": stamp, "end_kind": kind,
            "legs_at_end": legs_at_end,
            "result_usd": round(result, 2),
            "start_price": cluster_start_price,
            "end_price": round(close_price, 4),
        })
        # Both paths go through the core so the cluster reset stays in ONE
        # place - it also clears the sunk pot, which a hand-rolled
        # legs.clear() here did not.
        core.on_cluster_closed(toggle=(kind == "tp" and regime == "mode1"))
        cluster_start = None
        cluster_start_price = None
        if on_cluster is not None:
            on_cluster(clusters[-1])

    def open_leg(stamp: str, close: float, qty: int) -> None:
        nonlocal cluster_start, cluster_start_price
        core.book_settled(-cost_usd * qty)
        if style == "long_options":
            right = "call" if core.is_long else "put"
            picked = pick_contract(underlying, stamp, right, close,
                                   dte_min, dte_max, last_day, timeframe)
            if picked is None:
                raise RuntimeError(f"no tradable {right} on {stamp}")
            occ, expiry, entry_close = picked
            if not core.legs:
                cluster_start = stamp
                cluster_start_price = round(close, 4)
            core.add_leg(occ, qty, close, entry_close)
            leg_meta[occ] = {
                "kind": "long_option", "right": right,
                "expiry": expiry.isoformat(), "strike": int(occ[-8:]) / 1000.0,
                "closes": fetch_option_closes(
                    occ, stamp[:10], min(expiry.isoformat(), last_day), timeframe),
                "last_close": entry_close,
            }
            stats["premium_gross"] += entry_close * mult * qty
        elif style == "short_premium" and core.is_long:  # cash-secured put
            picked = pick_contract(underlying, stamp, "put", close,
                                   dte_min, dte_max, last_day, timeframe)
            if picked is None:
                raise RuntimeError(f"no tradable put on {stamp}")
            occ, expiry, entry_close = picked
            if not core.legs:
                cluster_start = stamp
                cluster_start_price = round(close, 4)
            core.add_leg(occ, qty, close, entry_close)   # credit received
            leg_meta[occ] = {
                "kind": "short_put", "right": "put",
                "expiry": expiry.isoformat(), "strike": int(occ[-8:]) / 1000.0,
                "closes": fetch_option_closes(
                    occ, stamp[:10], min(expiry.isoformat(), last_day), timeframe),
                "last_close": entry_close,
            }
            stats["premium_gross"] += entry_close * mult * qty
        else:                                    # credit-spread leg
            right = "put" if core.is_long else "call"
            widths = None
            if spread_width_pct:
                # Candidates around the requested percentage of spot, in
                # whole dollars because strikes are, nearest first.
                #
                # The list has to be DENSE, not a handful of multiples:
                # strike grids differ per symbol, and a sparse list simply
                # misses them. Measured 2026-08-30 on the first bar of the
                # 14.-27.08 window - AAPL at 306.10 offers only the strikes
                # 300/305/310, NVDA at 225.68 only 220/225/230, AMZN at
                # 264.77 only 260/265/270, all on a 5 USD grid. A 0.71 %
                # wing wants 2.17 USD there, and the old candidates
                # 2/3/1/4 hit nothing, so those three symbols aborted with
                # "no tradable credit spread". Every whole dollar from
                # 0.4x to 3x the wanted distance reaches a 5 USD grid while
                # still preferring the requested width.
                want = close * spread_width_pct / 100.0
                lo = max(1, int(round(want * 0.4)))
                hi = max(lo + 1, int(round(want * 3.0)))
                widths = tuple(sorted(range(lo, hi + 1),
                                      key=lambda w: (abs(w - want), w)))
            picked = pick_spread(underlying, stamp, right, close,
                                 dte_min, dte_max, last_day, timeframe,
                                 widths=widths)
            if picked is None:
                raise RuntimeError(
                    f"no tradable {right} credit spread on {stamp}")
            occ_s, strike_s, closes_s, occ_w, strike_w, closes_w, expiry = picked
            credit = closes_s[stamp] - closes_w[stamp]
            if not core.legs:
                cluster_start = stamp
                cluster_start_price = round(close, 4)
            core.add_leg(occ_s, qty, close, credit)      # net credit
            leg_meta[occ_s] = {
                "kind": f"{right}_spread", "right": right,
                "expiry": expiry.isoformat(), "strike": float(strike_s),
                "wing_strike": float(strike_w), "wing_occ": occ_w,
                "closes": closes_s, "last_close": closes_s[stamp],
                "wing_closes": closes_w, "wing_last_close": closes_w[stamp],
            }
            stats["premium_gross"] += credit * mult * qty
        stats["legs_opened"] += 1

    total_bars = len(bars)
    for bar_index, bar in enumerate(bars):
        day = bar["t"][:10]
        # bar key: date on 1Day (unchanged), full timestamp on 1Hour
        stamp = day if daily else bar["t"]
        # expiry settles on the day's LAST bar (on 1Day that IS the bar)
        last_bar_of_day = (bar_index + 1 == total_bars
                           or bars[bar_index + 1]["t"][:10] != day)
        range_pct = (bar["h"] - bar["l"]) / bar["l"] * 100.0
        if range_pct > RANGE_BLOWOUT_PCT:
            if day in KNOWN_DEFECT_DAYS:
                print(f"DATA-DEFECT skip {day}: {KNOWN_DEFECT_DAYS[day]}")
                continue
            raise RuntimeError(f"range blowout on {day}: {range_pct:.1f} %")
        close = bar["c"]

        # 1) expiry settlements (at the close of the expiry day, both modes)
        if core.legs:
            surviving = []
            for leg in core.legs:
                expiry = leg_meta[leg.option_symbol]["expiry"]
                if day > expiry or (day == expiry and last_bar_of_day):
                    core.book_settled(leg_settle(leg, close))
                else:
                    surviving.append(leg)
            core.legs[:] = surviving
            if not core.legs and core.sunk_pot != 0.0:
                end_cluster("expired", stamp, core.sunk_pot, 0, close)

        # 2) marks + take-profit on OPEN legs only
        closed_today = False
        if core.legs:
            for leg in core.legs:
                meta = leg_meta[leg.option_symbol]
                if stamp in meta["closes"]:
                    meta["last_close"] = meta["closes"][stamp]
                else:
                    stats["stale_marks"] += 1
                # BOTH spread kinds carry a protective wing whose mark must be
                # refreshed - leg_pnl() reads wing_last_close for either kind.
                if meta["kind"].endswith("_spread"):
                    if stamp in meta["wing_closes"]:
                        meta["wing_last_close"] = meta["wing_closes"][stamp]
                    else:
                        stats["stale_marks"] += 1
            open_pnl = sum(leg_pnl(l) for l in core.legs)
            if core.check_close(open_pnl, close, close):
                stats["legs_sold_tp"] += len(core.legs)
                exit_cost = cost_usd * sum(l.qty for l in core.legs)
                end_cluster("tp", stamp, core.sunk_pot + open_pnl - exit_cost,
                            len(core.legs), close)
                closed_today = True

        # 3) rebuy (never on the bar of a close - original ordering)
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
                    open_leg(stamp, close, qty)

        margin = sum(leg_margin(l) for l in core.legs)
        stats["max_margin"] = max(stats["max_margin"], margin)
        open_mark = (core.sunk_pot + sum(leg_pnl(l) for l in core.legs)
                     if core.legs else 0.0)
        equity.append((stamp, realized_total, open_mark))
        if on_day is not None:
            on_day(bar_index, total_bars, stamp, realized_total, open_mark)

    open_book = None
    if core.legs or core.sunk_pot:
        open_book = {
            "direction": "long" if core.is_long else "short",
            "start": cluster_start, "legs": core.invest_count,
            "start_price": cluster_start_price,
            "last_price": round(bars[-1]["c"], 4),
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
    parser.add_argument("--underlying", default="SPY")
    parser.add_argument("--cost_usd", type=float, default=0.0,
                        help="execution cost per spread per execution (USD)")
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
    parser.add_argument("--timeframe", choices=["1Day", "1Hour"],
                        default="1Day",
                        help="bar resolution; 1Hour needs an hourly --bars "
                             "store and forbids sma regimes (their SMA is a "
                             "200-DAY average)")
    parser.add_argument("--out", default="data")

    def _bool_arg(v: str) -> bool:
        # argparse's type=bool turns EVERY non-empty string (even "False")
        # into True - a silent-wrong trap. Parse explicitly, fail loud.
        s = str(v).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise argparse.ArgumentTypeError(f"expected true/false, got {v!r}")

    for key, val in DEFAULT_PARAMS.items():
        typ = _bool_arg if isinstance(val, bool) else type(val)
        parser.add_argument(f"--{key}", type=typ, default=val)
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

    if args.timeframe == "1Hour" and args.regime in ("sma200", "sma200_flat"):
        raise SystemExit("sma200 regimes are defined on the 200-DAY average; "
                         "an hourly bar store would silently turn it into a "
                         "200-hour one - not wired (fail loud).")
    result = run(bars, params, args.dte_min, args.dte_max,
                 underlying=args.underlying, sma=sma, style=args.style,
                 regime=args.regime, cost_usd=args.cost_usd,
                 timeframe=args.timeframe)
    summarize(result)

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "clusters_options.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "cluster_id", "direction", "start", "end", "end_kind",
            "legs_at_end", "result_usd", "start_price", "end_price"])
        writer.writeheader()
        writer.writerows(result["clusters"])
    print(f"clusters written: {path}")


if __name__ == "__main__":
    main()
