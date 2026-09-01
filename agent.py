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

"""Kangaroo Options agent - put-credit-spread grids on Alpaca paper trading.

MULTI-INSTRUMENT, in the loader shape the author's cTrader bots use
(QuantroTrader CLAUDE.md REGEL 0.7 / §13 "Loader-Sentinel-Pattern"):
config.yaml is the LOADER and carries the shared defaults; `config_path`
points at a folder holding ONE config per instrument, each separately
optimised and overriding only what differs. One process drives them all -
a single CLI, a single market clock, a single account, and one independent
grid per instrument with its own cluster state and its own state file.

    config_path: "_" or absent  -> one instrument, the loader's own values
    config_path: instruments/   -> one instrument per *.yaml in that folder

Configuration chosen from the backtest sweep of 2026-08-28 (SPY,
2024-02..2026-08, real Alpaca option prices): the PUT-ONLY grid selling PUT
CREDIT SPREADS was the only account-sized profitable variant; every
long-option variant was negative and the Mode1 short side lost money in
every measured style.

RE-MEASURED 2026-08-29 after the wing-mark defect fix (backtest_options.py
marked put-spread wings at their ENTRY price for the whole leg life, which
inflated every put-spread run): put-only +2,688 USD at -2,954 USD max
drawdown, margin peak 2,000 USD (was +34,484 / -2,721 before the fix).
Call-only is unaffected by the defect at -5,298 USD.

Strategy per leg: SELL the ATM option, BUY a protective wing ~5 $ further
out of the money (width probed over configured candidates) - one mleg
LIMIT order at a marketable net-credit limit (short bid - wing ask). The
contract side follows the instrument's `start_long`: true = put credit
spread (bullish), false = call credit spread (bearish); the wing sits
below the short strike for puts and above it for calls. The grid logic is
unchanged Kangaroo: rebuy on adverse underlying moves (falling for a put
grid, rising for a call grid) with 1.1^n growth, take-profit on the
cluster's aggregated closeable P&L, direction never toggles inside an
instrument - a symbol traded both ways gets two instrument configs.

Loop per poll, per instrument (close check first, never open in the same
iteration):
  0. assignment gate - an ASSIGNED underlying stock position is flattened
     at market before anything else, so no leg is settled while the stock
     it produced is still on the books
  1. expiry handling - a leg past its expiration is settled through
     kangaroo_core.settlement_pnl against the underlying close of the
     expiry day, and its realized USD goes into the cluster's SUNK POT
     (user rule 2026-08-28: let legs expire). While an assignment is still
     open the settlement is deferred rather than booked twice.
  2. market clock gate (outside regular hours: sample and wait)
  3. underlying quote -> trigger math (never option premiums)
  4. take-profit check on the WHOLE cluster - open legs' closeable P&L
     (buy back short at ask, sell wing at bid) PLUS the sunk pot, the same
     comparison the simulator makes and the same shape as the original's
     ClusterProfit = open positions + mClosedProfit. A cluster therefore
     never takes profit while its total is negative. On TP every spread is
     closed and the grid restarts in the same direction.
  5. rebuy check -> sell one new put credit spread

Orders: mleg LIMIT day orders (order form verified on the paper account
2026-08-28). An order that does not fill within fill_requote_samples polls
is canceled BY ID and re-quoted on the next loop - never left dangling,
never cancel-all. Every client_order_id carries the INSTRUMENT, because
two instruments reach the same cluster/leg counter independently. Log
lines describing market decisions carry the QUOTE timestamp, not the wall
clock, and are prefixed with the instrument.

Usage:
  python agent.py --config config.yaml [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import traceback
from datetime import date, timedelta

import yaml

from alpaca_cli import AlpacaCli, AlpacaCliError
from risk_gate import RiskGate
from fetch_alpaca_bars import fetch as fetch_bars
from kangaroo_core import KangarooCore, settlement_pnl

TERMINAL_BAD = {"canceled", "expired", "rejected", "done_for_day",
                "stopped", "suspended", "replaced"}

# Keys the loader supplies once for the whole run; an instrument config may
# not override them because they describe the PROCESS, not the grid.
PROCESS_KEYS = ("cli_path", "env_file", "poll_seconds", "poll_fill_seconds",
                "fill_requote_samples", "config_path", "risk_gate")


# Consecutive failed polls before an instrument is halted for the rest of
# the run. One transient failure (a quote gap, a throttle) must not stop a
# grid that holds positions; a persistent one must not keep retrying an
# order every few seconds for days.
HALT_AFTER = 20


class SingleInstance:
    """An exclusive lock on one file, released by the OS on process death.

    Windows and POSIX expose this differently (msvcrt.locking against
    fcntl.flock) but both give the same guarantee, which a PID file does
    not: a process that is killed -9, bluescreens or loses power leaves NO
    lock behind, while a live one cannot be doubled. That matters because
    the watchdog's whole job is to start the agent when it looks dead.
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self.handle = None

    # msvcrt.locking() locks ONE BYTE AT THE CURRENT FILE POSITION, and a
    # locked byte cannot be written even through the handle that holds the
    # lock. Locking byte 0 and then writing the pid there fails with
    # PermissionError - measured 2026-08-31. So the lock sits far past any
    # content: Windows happily locks a region beyond end-of-file, and the
    # pid is written at offset 0 where nothing is locked.
    LOCK_OFFSET = 1 << 20

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            open(self.path, "a").close()
        # "r+" and not "a+": in append mode every write goes to the end of
        # the file regardless of seek, so the pid would pile up instead of
        # replacing the previous one.
        self.handle = open(self.path, "r+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(self.LOCK_OFFSET)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            try:
                with open(self.path, "r") as fh:
                    other = fh.read(64).strip() or "unknown"
            except OSError:
                other = "unknown"
            raise AlpacaCliError(
                f"another agent already holds {self.path} (it recorded "
                f"pid {other}). Two processes on one set of state files "
                f"would each overwrite the other's cluster - refusing to "
                f"start. Stop that process first.")
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(self.LOCK_OFFSET)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


def log(message: str, data_ts: str | None = None,
        who: str | None = None) -> None:
    prefix = f"[{data_ts}] " if data_ts else ""
    tag = f"{who}: " if who else ""
    print(f"{prefix}{tag}{message}", flush=True)


def atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def load_instrument_configs(loader: dict, config_dir: str) -> list[dict]:
    """The loader's instruments, in the cTrader ConfigPath shape.

    `config_path` absent or "_" -> a single instrument built from the
    loader itself (the behaviour before multi-instrument). Otherwise every
    *.yaml in that folder is one instrument: it inherits the loader's
    values and overrides what it names, exactly like a per-symbol cbotset
    overriding a loader cbotset. The folder path is resolved relative to
    the loader config, never to the current working directory - a start
    from the wrong folder must not silently trade a different set.
    """
    raw = loader.get("config_path")
    if raw in (None, "", "_"):
        return [dict(loader)]
    folder = raw if os.path.isabs(raw) else os.path.join(config_dir, raw)
    if not os.path.isdir(folder):
        raise AlpacaCliError(f"config_path is not a folder: {folder}")
    files = sorted(glob.glob(os.path.join(folder, "*.yaml"))
                   + glob.glob(os.path.join(folder, "*.yml")))
    if not files:
        raise AlpacaCliError(f"config_path holds no *.yaml: {folder}")
    out = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            own = yaml.safe_load(fh) or {}
        clash = [k for k in PROCESS_KEYS if k in own]
        if clash:
            raise AlpacaCliError(
                f"{os.path.basename(path)} sets loader-only key(s) "
                f"{clash} - those describe the process, not the grid")
        merged = dict(loader)
        merged.pop("config_path", None)
        # state_file is per instrument. Inheriting one from the loader
        # would hand the SAME file to every instrument, so each would load
        # the previous one's cluster and overwrite it on the next save -
        # silently, with the positions of several symbols in one grid. An
        # instrument may still name its own below.
        if "state_file" in merged:
            raise AlpacaCliError(
                "the loader config sets state_file while config_path names "
                "a folder: that one file would be shared by every "
                "instrument. Drop it - each instrument defaults to "
                "state/kangaroo_<symbol>_<side>.json - or set it inside "
                "the individual configs.")
        merged.update(own)
        merged["config_file"] = path
        out.append(merged)
    # Identity is (underlying, direction), not underlying: one symbol may
    # carry a put-credit-spread grid AND a call-credit-spread grid, which
    # hold different contracts. Two grids on the SAME side of one symbol
    # would fight over the same positions, so that stays an error.
    names = [f"{c['underlying']}_{'long' if c.get('start_long', True) else 'short'}"
             for c in out]
    if len(set(names)) != len(names):
        raise AlpacaCliError(
            f"config_path holds two configs for the same underlying AND "
            f"direction: {sorted(names)}")
    return out


class Instrument:
    """One underlying: its own grid, its own cluster state, its own file.

    Holds no I/O of its own - the CLI and the market clock belong to the
    loader and are handed in, so N instruments cost one clock call per
    poll, not N.
    """

    def __init__(self, config: dict, cli: AlpacaCli, dry_run: bool,
                 config_dir: str) -> None:
        self.cfg = config
        self.cli = cli
        self.dry_run = dry_run
        self.underlying = config["underlying"]
        # Direction of the grid. start_long=True is the BULLISH cluster,
        # which in the credit-spread style is a PUT spread (the simulator
        # makes the same mapping: backtest_options.py `right = "put" if
        # core.is_long else "call"`). Adverse for it is a falling
        # underlying, which is what KangarooCore.check_rebuy tests.
        self.start_long = bool(config.get("start_long", True))
        self.side = "long" if self.start_long else "short"
        self.right = "put" if self.start_long else "call"
        self.spread_kind = "put_spread" if self.start_long else "call_spread"
        # The wing is further out of the money than the short strike: below
        # it for puts, above it for calls.
        self.wing_sign = -1 if self.start_long else 1
        # Name = the instrument's identity everywhere a symbol alone would
        # collide between the two directions: log prefix, state file,
        # client_order_id.
        self.name = f"{self.underlying}_{self.side}"
        self.poll_fill_seconds = float(config["poll_fill_seconds"])
        self.fill_requote_samples = int(config["fill_requote_samples"])
        self.dte_min = int(config["dte_min"])
        self.dte_max = int(config["dte_max"])
        self.spread_widths = [int(w) for w in config["spread_widths"]]
        # State path: per instrument and ABSOLUTE. A relative path resolved
        # against the working directory means a start from the wrong folder
        # finds no state, opens a second grid on top of the live one and
        # never notices.
        raw = config.get("state_file") or os.path.join(
            "state", f"kangaroo_{self.underlying.lower()}_{self.side}.json")
        self.state_file = raw if os.path.isabs(raw) else os.path.abspath(
            os.path.join(config_dir, raw))
        self.core = KangarooCore(
            rebuy_1st_pct=config["rebuy_1st_pct"],
            rebuy_pct=config["rebuy_pct"],
            tp_pct=config["tp_pct"],
            initial_qty=config["initial_qty"],
            max_invest_count=config["max_invest_count"],
            start_long=self.start_long,
            max_adverse_pct=config["max_adverse_pct"],
        )
        # per-leg spread details, keyed by the SHORT leg's OCC symbol
        self.leg_extras: dict[str, dict] = {}
        self._market_was_open: bool | None = None
        # Consecutive failed polls. Reset by the first clean one; at
        # HALT_AFTER the instrument stops for the rest of the run.
        self.failures = 0
        self.halted = False

    def say(self, message: str, data_ts: str | None = None) -> None:
        log(message, data_ts, who=self.name)

    # --- state persistence ----------------------------------------------

    def load_state(self) -> None:
        if not os.path.isfile(self.state_file):
            return
        with open(self.state_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.core.restore(payload["core"])
        self.leg_extras = payload["leg_extras"]
        stored = payload.get("name") or payload.get("underlying")
        if stored and stored != self.name:
            raise AlpacaCliError(
                f"state file {self.state_file} belongs to {stored}, not "
                f"{self.name} - refusing to trade another instrument's "
                f"cluster")
        for leg in self.core.legs:
            if leg.option_symbol not in self.leg_extras:
                raise AlpacaCliError(
                    f"state file has no spread details for {leg.option_symbol}")
        self.say(f"state restored: cluster {self.core.cluster_id} "
                 f"legs={self.core.invest_count}")

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        extras = {leg.option_symbol: self.leg_extras[leg.option_symbol]
                  for leg in self.core.legs}
        atomic_write_json(self.state_file,
                          {"name": self.name,
                           "underlying": self.underlying,
                           "start_long": self.start_long,
                           "core": self.core.to_dict(),
                           "leg_extras": extras})

    def recover_unbooked_close(self, positions: dict,
                               orders: list[dict]) -> bool:
        """Book a close the broker filled but this agent never recorded.

        Returns True when the cluster was advanced. Two conditions have to
        hold together, and neither alone is enough:
          - a FILLED order whose client_order_id names THIS instrument and
            THIS cluster as a close (`_x_`), and
          - not one of the cluster's legs still present in the account.
        The order alone could belong to a partially closed cluster; the
        missing position alone could be a broker lag or a bad state file,
        and silently discarding a real cluster on that basis would be far
        worse than halting.
        """
        if not self.core.legs:
            return False
        for leg in self.core.legs:
            extra = self.leg_extras.get(leg.option_symbol) or {}
            for occ in (leg.option_symbol, extra.get("wing_occ")):
                if occ and occ in positions:
                    return False          # something is still held
        prefix = f"kang_{self.name}_c{self.core.cluster_id}_"
        closes = [o for o in orders
                  if o.get("status") == "filled"
                  and (o.get("client_order_id") or "").startswith(prefix)
                  and "_x_" in (o.get("client_order_id") or "")]
        if not closes:
            return False
        ended = self.core.cluster_id
        realized = self.core.sunk_pot
        self.core.legs.clear()
        self.core.on_cluster_closed(toggle=False)
        self.leg_extras = {}
        self.save_state()
        self.say(
            f"RECOVERED cluster {ended}: the broker filled its close "
            f"({len(closes)} order(s), latest "
            f"{closes[-1].get('client_order_id')}) but this agent was "
            f"stopped before it could book it. No position of that cluster "
            f"remains in the account. Booked {realized:+.2f} USD of already "
            f"realized legs and moved on to cluster "
            f"{self.core.cluster_id}.")
        return True

    def reconcile(self, positions: dict) -> None:
        """Every spread leg in the state must exist in the account: the
        short put as a short position, the wing as a long position, each
        with at least the leg's quantity. Any mismatch aborts. `positions`
        is the account-wide snapshot the loader read once for all
        instruments."""
        if not self.core.legs:
            return

        def held(occ: str, want_short: bool, qty: int) -> bool:
            p = positions.get(occ)
            if p is None:
                return False
            signed = float(p["qty"])
            is_short = signed < 0 or "short" in str(p.get("side", "")).lower()
            return is_short == want_short and abs(signed) >= qty

        for leg in self.core.legs:
            extra = self.leg_extras[leg.option_symbol]
            if not held(leg.option_symbol, True, leg.qty):
                raise AlpacaCliError(
                    f"{self.underlying}: state/positions mismatch: short leg "
                    f"{leg.option_symbol} x{leg.qty} not present as short - "
                    f"refusing to trade")
            if not held(extra["wing_occ"], False, leg.qty):
                raise AlpacaCliError(
                    f"{self.underlying}: state/positions mismatch: wing "
                    f"{extra['wing_occ']} x{leg.qty} not present as long - "
                    f"refusing to trade")

    # --- order helpers ---------------------------------------------------

    def coid(self, kind: str, leg_index: int) -> str:
        """Client order id, UNIQUE PER SUBMISSION.

        Carries the INSTRUMENT, symbol AND direction: instruments reach the
        same cluster and leg counter independently, so without that a long
        and a short grid on one symbol collide on the same id in one
        account.

        The millisecond stamp is what makes it unique per ATTEMPT, and that
        is not cosmetic: an unfilled limit order is canceled by id after
        fill_requote_samples polls and re-quoted on the next loop. Re-using
        the id there is rejected by the broker - measured live 2026-08-31,
        HTTP 422 code 40010001 "client_order_id must be unique" - and the
        instrument then fails EVERY poll, walking towards its halt while
        the grid never opens. A per-process counter would collide again
        after a restart; a timestamp does not."""
        return (f"kang_{self.name}_c{self.core.cluster_id}"
                f"_l{leg_index}_{kind}_{int(time.time() * 1000)}")

    def wait_filled_or_cancel(self, order_id: str) -> dict | None:
        """Poll one order until filled. After fill_requote_samples polls the
        order is canceled BY ID and None is returned (the caller re-quotes
        on the next loop). A terminal non-filled status is an error."""
        for _ in range(self.fill_requote_samples):
            order = self.cli.order_get(order_id)
            status = order["status"]
            if status == "filled":
                return order
            if status in TERMINAL_BAD:
                raise AlpacaCliError(f"order {order_id} ended '{status}': {order}")
            time.sleep(self.poll_fill_seconds)
        self.cli.cancel_order(order_id)
        while True:                     # await the cancel, event-based
            order = self.cli.order_get(order_id)
            if order["status"] == "filled":
                return order            # raced a late fill - accept it
            if order["status"] in TERMINAL_BAD:
                self.say(f"order {order_id} canceled unfilled - will re-quote")
                return None
            time.sleep(self.poll_fill_seconds)

    @staticmethod
    def _leg_fills(order: dict, occ_short: str, occ_wing: str) -> tuple[float, float]:
        legs = order.get("legs") or []
        fills = {l.get("symbol"): l.get("filled_avg_price") for l in legs}
        if not fills.get(occ_short) or not fills.get(occ_wing):
            raise AlpacaCliError(
                f"mleg order filled but per-leg fills are missing: {order}")
        return float(fills[occ_short]), float(fills[occ_wing])

    # --- spread selection ------------------------------------------------

    def select_spread(self, spot_mid: float, today: date,
                      positions: dict | None = None) -> dict:
        """Nearest expiration inside [dte_min, dte_max]; short strike
        nearest to spot; wing = the NEAREST EXISTING strike to each
        configured width, further out of the money than the short strike
        (below it for puts, above it for calls). The contract side follows
        the grid direction. Prices come from live quotes.

        NEAREST, not exact - this is the rule backtest_options.pick_spread
        uses (`min(wing_side, key=lambda k: abs(k - want))`), and the two
        have to agree or the bot trades something other than what was
        measured. An exact-match rule cost TLT_long its whole day on
        2026-08-31: with the short strike at 82.5 and a chain of whole
        dollars below 80 but half dollars above, every configured width
        landed on a half dollar that does not exist, and the instrument was
        halted at 20 failed polls without ever opening.

        Every distinct candidate is priced in ONE quote request and the
        first with a positive marketable credit wins - the simulator also
        requires a positive net credit before it accepts a pair."""
        contracts = self.cli.option_contracts(
            self.underlying,
            (today + timedelta(days=self.dte_min)).isoformat(),
            (today + timedelta(days=self.dte_max)).isoformat(),
            self.right,
        )
        tradable = [c for c in contracts if c["tradable"]]
        if not tradable:
            raise AlpacaCliError(
                f"no tradable {self.right}s for {self.underlying} in "
                f"DTE [{self.dte_min},{self.dte_max}]")
        expiry = min(c["expiration_date"] for c in tradable)
        chain = {float(c["strike_price"]): c
                 for c in tradable if c["expiration_date"] == expiry}
        # Skip anything that would NET against a position we already
        # hold instead of opening a new one. Selling a contract we are long
        # closes that long; the broker says so (422 "position intent
        # mismatch, inferred: sell_to_close") and it is right. Measured on
        # GOOGL_long, whose rebuy stepped onto its own leg-1 wing.
        held = positions or {}

        def qty_of(strike):
            c = chain.get(strike)
            p = held.get(c["symbol"]) if c else None
            return float(p["qty"]) if p else 0.0

        usable = [k for k in chain if qty_of(k) <= 0]     # not held long
        if not usable:
            raise AlpacaCliError(
                f"every strike at {expiry} for {self.underlying} is already "
                f"held long - selling any of them would close a wing, not "
                f"open a leg")
        short_strike = min(usable, key=lambda s: abs(s - spot_mid))
        wing_side = [k for k in chain
                     if (k > short_strike if self.wing_sign > 0
                         else k < short_strike)]
        # A wing we are SHORT would likewise net instead of hedging.
        wing_side = [k for k in wing_side if qty_of(k) >= 0]
        if not wing_side:
            raise AlpacaCliError(
                f"no usable strike "
                f"{'below' if self.wing_sign < 0 else 'above'} "
                f"{short_strike} at {expiry} for {self.underlying} "
                f"(a strike held short cannot serve as a wing)")
        candidates, seen = [], set()
        for width in self.spread_widths:
            want = short_strike + self.wing_sign * width
            nearest = min(wing_side, key=lambda k: abs(k - want))
            if nearest not in seen:
                seen.add(nearest)
                candidates.append(nearest)
        occ_s = chain[short_strike]["symbol"]
        wing_occs = [chain[k]["symbol"] for k in candidates]
        quotes = self.cli.option_quotes([occ_s] + wing_occs)
        if occ_s not in quotes:
            raise AlpacaCliError(f"no quote for {occ_s}")
        wing_strike, occ_w, credit_limit = None, None, None
        rejected = []
        for k, occ in zip(candidates, wing_occs):
            if occ not in quotes:
                rejected.append(f"{k}: no quote")
                continue
            credit = quotes[occ_s]["bp"] - quotes[occ]["ap"]
            if credit > 0:
                wing_strike, occ_w, credit_limit = k, occ, credit
                break
            rejected.append(f"{k}: credit {credit:+.2f}")
        if occ_w is None:
            raise AlpacaCliError(
                f"no wing with a positive marketable credit against "
                f"{occ_s} at {expiry} - tried {', '.join(rejected)}")
        return {
            "occ_short": occ_s, "occ_wing": occ_w,
            "strike": short_strike, "wing_strike": wing_strike,
            "expiry": expiry, "credit_limit": round(credit_limit, 2),
            "quote_ts": quotes[occ_s]["t"],
        }

    # --- trading actions -------------------------------------------------

    def open_leg(self, qty: int, spot_bid: float, spot_ask: float,
                 today: date, positions: dict | None = None) -> None:
        spread = self.select_spread((spot_bid + spot_ask) / 2.0, today,
                                    positions)
        coid = self.coid("o", self.core.invest_count)
        legs = [
            {"symbol": spread["occ_short"], "ratio_qty": "1",
             "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": spread["occ_wing"], "ratio_qty": "1",
             "side": "buy", "position_intent": "buy_to_open"},
        ]
        limit = -spread["credit_limit"]        # negative = minimum credit
        if self.dry_run:
            body = self.cli.submit_mleg_limit(legs, qty, limit, coid,
                                              dry_run=True)
            self.say(f"DRY-RUN would sell {qty}x put credit spread "
                     f"{spread['occ_short']}/{spread['occ_wing']} "
                     f"limit {limit}: {body}", spread["quote_ts"])
            return
        order = self.cli.submit_mleg_limit(legs, qty, limit, coid)
        filled = self.wait_filled_or_cancel(order["id"])
        if filled is None:
            return                              # re-quote on the next loop
        fill_s, fill_w = self._leg_fills(filled, spread["occ_short"],
                                         spread["occ_wing"])
        net_credit = fill_s - fill_w
        self.core.add_leg(spread["occ_short"], qty,
                          self.core.entry_reference(spot_bid, spot_ask),
                          net_credit)
        self.leg_extras[spread["occ_short"]] = {
            "wing_occ": spread["occ_wing"], "strike": spread["strike"],
            "wing_strike": spread["wing_strike"], "expiry": spread["expiry"],
        }
        self.save_state()
        self.say(f"OPEN leg {self.core.invest_count}/"
                 f"{self.core.max_invest_count} cluster "
                 f"{self.core.cluster_id}: sold {qty}x "
                 f"{spread['occ_short']}/{spread['occ_wing']} "
                 f"credit {net_credit:.2f} (underlying "
                 f"{self.core.legs[-1].entry_underlying})", spread["quote_ts"])

    def leg_symbols(self) -> list[str]:
        """Every OCC symbol this instrument currently holds, short legs and
        wings. The loader collects these across instruments to ask for all
        option quotes in one request."""
        out = []
        for leg in self.core.legs:
            out += [leg.option_symbol,
                    self.leg_extras[leg.option_symbol]["wing_occ"]]
        return out

    def closeable_pnl(self, quotes: dict) -> tuple[float, dict, str]:
        """(cluster P&L closeable NOW, per-leg net close costs, quote ts).
        Close cost per spread = short ask - wing bid. `quotes` is the
        loader's shared option-quote map, so this costs no request."""
        total, costs, ts = 0.0, {}, None
        for leg in self.core.legs:
            occ_w = self.leg_extras[leg.option_symbol]["wing_occ"]
            for occ in (leg.option_symbol, occ_w):
                if occ not in quotes:
                    raise AlpacaCliError(f"no quote for open leg part {occ}")
            net_close = (quotes[leg.option_symbol]["ap"]
                         - quotes[occ_w]["bp"])
            costs[leg.option_symbol] = round(net_close, 2)
            total += ((leg.entry_premium - net_close)
                      * self.core.contract_multiplier * leg.qty)
            ts = quotes[leg.option_symbol]["t"]
        return total, costs, ts

    def close_cluster(self, costs: dict, quote_ts: str) -> None:
        if self.dry_run:
            self.say(f"DRY-RUN would close cluster {self.core.cluster_id} "
                     f"({self.core.invest_count} legs)", quote_ts)
            return
        remaining = list(self.core.legs)
        for i, leg in enumerate(remaining):
            extra = self.leg_extras[leg.option_symbol]
            coid = self.coid("x", i)
            legs = [
                {"symbol": leg.option_symbol, "ratio_qty": "1",
                 "side": "buy", "position_intent": "buy_to_close"},
                {"symbol": extra["wing_occ"], "ratio_qty": "1",
                 "side": "sell", "position_intent": "sell_to_close"},
            ]
            limit = costs[leg.option_symbol]    # positive = maximum debit
            order = self.cli.submit_mleg_limit(legs, leg.qty, limit, coid)
            filled = self.wait_filled_or_cancel(order["id"])
            if filled is None:
                # partial close is a consistent state: keep the rest,
                # the TP check will fire again on the next loop
                self.say(f"close of {leg.option_symbol} did not fill - "
                         f"keeping remaining legs, re-quoting next loop",
                         quote_ts)
                self.save_state()
                return
            fill_s, fill_w = self._leg_fills(filled, leg.option_symbol,
                                             extra["wing_occ"])
            # Into the POT, not a local: when a later leg's close does not
            # fill, this method returns with the cluster still alive, and
            # the money already taken has to stay with it. In a local float
            # it was dropped, and the next take-profit check then measured
            # the surviving legs against a cluster total that had lost its
            # realized part.
            self.core.book_settled((leg.entry_premium - (fill_s - fill_w))
                                   * self.core.contract_multiplier * leg.qty)
            self.core.legs.remove(leg)
            self.save_state()
        ended, realized = self.core.cluster_id, self.core.sunk_pot
        # No Mode1: an instrument keeps its direction for its whole life.
        self.core.on_cluster_closed(toggle=False)
        self.save_state()
        self.say(f"CLOSE cluster {ended}: realized {realized:.2f} USD "
                 f"- restarting {self.right} grid "
                 f"(cluster {self.core.cluster_id})",
                 quote_ts)

    # --- gates -----------------------------------------------------------

    def settlement_close(self, expiry: str) -> float:
        """Underlying close of the expiry day - the price the expiring legs
        settle against, exactly as the simulator does it. Fails loud when
        the bar is missing: guessing a settlement price would silently move
        the cluster's whole pot."""
        bars = fetch_bars(self.underlying, "1Day", expiry, expiry)
        if not bars:
            raise AlpacaCliError(
                f"no {self.underlying} daily bar for expiry {expiry} - "
                f"cannot settle the expired leg(s); refusing to guess")
        close = float(bars[-1]["c"])
        if close <= 0:
            raise AlpacaCliError(
                f"{self.underlying} close on {expiry} is {close}")
        return close

    def handle_expiries(self, clock_date: str, positions: dict) -> None:
        """User rule: let legs expire. A leg past its expiration date was
        settled by the broker overnight; book its realized USD into the
        cluster's sunk pot through the SAME formula the simulator uses
        (kangaroo_core.settlement_pnl), so the take-profit comparison here
        and in the backtest run on the same number.

        An ITM short put is ASSIGNED rather than cash-settled: the account
        then holds stock, and booking an intrinsic settlement while that
        position is open would count the same money twice. Settlement is
        therefore deferred until the account is flat in the underlying -
        assignment_gate flattens it on the next open poll."""
        expired = [l for l in self.core.legs
                   if clock_date > self.leg_extras[l.option_symbol]["expiry"]]
        if not expired:
            return
        held = positions.get(self.underlying)
        if held:
            self.say(f"{len(expired)} expired leg(s) pending, but the account "
                     f"holds {held['qty']} {self.underlying} from an "
                     f"assignment - settlement deferred until flat")
            return
        for leg in expired:
            extra = self.leg_extras[leg.option_symbol]
            close = self.settlement_close(extra["expiry"])
            pnl, itm = settlement_pnl(
                self.spread_kind, self.right,
                extra["strike"], extra["wing_strike"],
                leg.entry_premium, leg.qty, close,
                self.core.contract_multiplier)
            self.core.book_settled(pnl)
            self.say(f"EXPIRED leg {leg.option_symbol} x{leg.qty} "
                     f"{'ITM' if itm else 'OTM'} at {self.underlying} "
                     f"{close:.2f} on {extra['expiry']}: {pnl:+.2f} USD "
                     f"booked into cluster {self.core.cluster_id} "
                     f"(pot {self.core.sunk_pot:+.2f})")
            self.core.legs.remove(leg)
        if not self.core.legs:
            ended, booked = self.core.cluster_id, self.core.sunk_pot
            self.core.on_cluster_closed(toggle=False)
            self.say(f"cluster {ended} fully expired: {booked:+.2f} USD "
                     f"realized - restarting put grid "
                     f"(cluster {self.core.cluster_id})")
        self.save_state()

    def assignment_gate(self, positions: dict) -> None:
        """An assigned stock position is flattened immediately (user rule
        from the reference options bot: no wheel, close assignments)."""
        p = positions.get(self.underlying)
        if p is None:
            return
        signed = float(p["qty"])
        side = "sell" if signed > 0 else "buy"
        self.say(f"ASSIGNMENT GATE: found {signed} {self.underlying} - "
                 f"flattening at market")
        if self.dry_run:
            return
        order = self.cli.submit_equity_market(
            self.underlying, abs(signed), side,
            f"kang_assign_flat_{self.underlying}")
        while True:                 # market order: await the fill
            o = self.cli.order_get(order["id"])
            if o["status"] == "filled":
                self.say(f"assignment flattened: {o.get('filled_avg_price')}")
                return
            if o["status"] in TERMINAL_BAD:
                raise AlpacaCliError(f"assignment flatten failed: {o}")
            time.sleep(self.poll_fill_seconds)

    # --- one decision pass ------------------------------------------------

    def depth(self, spot: float) -> dict:
        """This cluster in the few numbers a portfolio decision needs.

        Also used to build the portfolio-wide view, so every instrument is
        described to the model in exactly the same terms as the one being
        decided about.
        """
        anchor = self.core.cluster_anchor
        adverse = None
        if anchor:
            moved = (anchor - spot) if self.core.is_long else (spot - anchor)
            adverse = round(moved / anchor * 100.0, 3)
        return {"instrument": self.name,
                "direction": "long" if self.core.is_long else "short",
                "legs": len(self.core.legs),
                "leg_index": self.core.invest_count,
                "contracts_open": sum(leg.qty for leg in self.core.legs),
                "adverse_pct_from_anchor": adverse,
                "max_adverse_pct": self.core.max_adverse_pct or None}

    def step(self, clock: dict, positions: dict, stock_quotes: dict,
             option_quotes: dict, gate=None, portfolio: dict | None = None
             ) -> None:
        """One decision pass. The clock, the position list and BOTH quote
        maps are read ONCE per poll by the loader and shared, so a poll
        costs four requests no matter how many instruments there are."""
        # Assignment BEFORE settlement: an assigned short put leaves stock
        # in the account, and handle_expiries must not book an intrinsic
        # settlement while that position is open. Flattening needs an open
        # market; handle_expiries defers over the hours in between.
        if clock["is_open"]:
            self.assignment_gate(positions)
        self.handle_expiries(clock["timestamp"][:10], positions)
        if not clock["is_open"]:
            if self._market_was_open is not False:
                self.say(f"market closed - next open {clock['next_open']} "
                         f"(clock {clock['timestamp']})")
                self._market_was_open = False
            return
        if self._market_was_open is not True:
            self.say(f"market open - next close {clock['next_close']} "
                     f"(clock {clock['timestamp']})")
            self._market_was_open = True

        quote = stock_quotes.get(self.underlying)
        if quote is None:
            raise AlpacaCliError(
                f"no quote for {self.underlying} in the batch "
                f"{sorted(stock_quotes)}")
        spot_bid, spot_ask, quote_ts = quote["bp"], quote["ap"], quote["t"]
        if not (spot_bid > 0 and spot_ask > 0):
            raise AlpacaCliError(
                f"invalid {self.underlying} quote: {quote}")

        if self.core.legs:
            pnl, costs, opt_ts = self.closeable_pnl(option_quotes)
            if self.core.check_close(pnl, spot_bid, spot_ask):
                self.close_cluster(costs, opt_ts)
                return  # never open in the same iteration as a close

        qty = self.core.check_rebuy(spot_bid, spot_ask)
        if qty and gate is not None:
            spot = (spot_bid + spot_ask) / 2.0
            view = dict(portfolio or {})
            view["this_cluster"] = self.depth(spot)
            view["spot"] = round(spot, 4)
            view["leg_index"] = self.core.invest_count
            decision = gate.review(qty, view)
            # Logged whenever a model was actually involved, not only when
            # it changed something: "the gate agreed" and "the gate was
            # down" must be distinguishable afterwards.
            if decision.source != "passthrough":
                self.say(f"RISK GATE [{decision.source}] "
                         f"{decision.requested} -> {decision.allowed} "
                         f"({decision.latency_ms} ms): {decision.reason}",
                         quote_ts)
            qty = decision.allowed
        if qty:
            self.open_leg(qty, spot_bid, spot_ask,
                          date.fromisoformat(clock["timestamp"][:10]),
                          positions)


class KangarooAgent:
    """The loader: one CLI, one clock, one account, N instruments."""

    def __init__(self, config: dict, dry_run: bool,
                 config_path: str) -> None:
        self.cfg = config
        self.dry_run = dry_run
        self.poll_seconds = float(config["poll_seconds"])
        self.cli = AlpacaCli(config["cli_path"], config.get("env_file"))
        # The gate's key defaults to the file the Alpaca credentials are
        # already in: one secret file on the host, one ACL. An explicit
        # risk_gate.key_file still wins.
        gate_cfg = dict(config.get("risk_gate") or {})
        gate_cfg.setdefault("key_file", config.get("env_file"))
        self.gate = RiskGate(gate_cfg, say=log)
        self._reported_halted: list[str] = []
        config_dir = os.path.dirname(os.path.abspath(config_path))
        self.instruments = [
            Instrument(c, self.cli, dry_run, config_dir)
            for c in load_instrument_configs(config, config_dir)
        ]

    def positions_by_symbol(self) -> dict:
        return {p["symbol"]: p for p in self.cli.positions()}

    def portfolio_view(self, account: dict, stock_quotes: dict) -> dict:
        """The whole account in the terms the gate reasons about.

        Built once per poll and shared by every instrument, so the model
        sees one consistent picture rather than 25 slightly different ones.
        `headroom_pct` is options buying power as a percentage of equity -
        what is left to open with, against what is at stake.
        """
        equity = float(account["equity"])
        obp = float(account.get("options_buying_power") or 0.0)
        rows = []
        for inst in self.instruments:
            quote = stock_quotes.get(inst.underlying) or {}
            bid, ask = quote.get("bp"), quote.get("ap")
            spot = (bid + ask) / 2.0 if bid and ask else 0.0
            if inst.core.legs:
                rows.append(inst.depth(spot))
        return {
            "equity": round(equity, 2),
            "cash": round(float(account["cash"]), 2),
            "options_buying_power": round(obp, 2),
            "maintenance_margin": round(
                float(account.get("maintenance_margin") or 0.0), 2),
            "headroom_pct": round(obp / equity * 100.0, 1) if equity else None,
            "clusters_open": len(rows),
            "instruments_total": len(self.instruments),
            "open_clusters": sorted(
                rows, key=lambda r: -(r["adverse_pct_from_anchor"] or 0.0)),
        }

    def market_data(self) -> tuple[dict, dict]:
        """(stock quotes, option quotes) for every instrument, in at most
        two requests. The option call is skipped entirely while no
        instrument holds a leg."""
        underlyings = sorted({i.underlying for i in self.instruments})
        stock = self.cli.stock_quotes(underlyings)
        occs = sorted({o for i in self.instruments for o in i.leg_symbols()})
        options = self.cli.option_quotes(occs) if occs else {}
        return stock, options

    def run(self, once: bool) -> None:
        account = self.cli.account()
        log(f"account {account['account_number']} "
            f"equity={account['equity']} "
            f"options_approved_level={account['options_approved_level']} "
            f"options_trading_level={account['options_trading_level']}")
        clock = self.cli.clock()
        positions = self.positions_by_symbol()
        # One order query for all instruments, covering today. A close the
        # agent failed to book can only have happened while it was running,
        # so the session's own day is the whole search space.
        recent_orders = self.cli.orders_since(clock["timestamp"][:10])
        log(f"broker reports {len(recent_orders)} order(s) since "
            f"{clock['timestamp'][:10]}")
        for inst in self.instruments:
            # Startup is where a RESTART fails: a state file that no longer
            # matches the account stops the instrument it belongs to, not
            # the other 24 - those may hold open positions that need
            # managing right now. Same rule as the poll loop: the
            # instrument stops, loudly, and is named in the summary.
            try:
                inst.load_state()
                # Settle first, THEN reconcile: an expired leg is gone from
                # the account, so reconcile would reject the state file it
                # just read and the agent could never restart after a
                # settlement failure.
                inst.handle_expiries(clock["timestamp"][:10], positions)
                # Ask the BROKER before declaring the state inconsistent:
                # a close it filled while this agent was being stopped is
                # not a mismatch, it is a cluster that ended.
                inst.recover_unbooked_close(positions, recent_orders)
                inst.reconcile(positions)
            except Exception:                                # noqa: BLE001
                inst.halted = True
                inst.say(f"HALTED AT STARTUP - it will not trade in this "
                         f"run; its positions and state file are untouched "
                         f"and need a human:\n{traceback.format_exc()}")
                continue
            log(f"grid ready: cluster {inst.core.cluster_id}, "
                f"legs={inst.core.invest_count}, "
                f"state={os.path.basename(inst.state_file)}",
                who=inst.name)
        live = [i.name for i in self.instruments if not i.halted]
        dead = [i.name for i in self.instruments if i.halted]
        c = self.gate.counters()
        if c["enabled"]:
            # read_key(), not os.environ: the key normally arrives through
            # the host's credentials file, and a banner that only looks at
            # the environment would report a working gate as broken.
            found = self.gate.read_key()
            where = self.gate.key_env if os.environ.get(self.gate.key_env)                 else os.path.basename(self.gate.key_file or '')
            log(f"risk gate: {c['provider']} {c['model']}, consulted from "
                f"leg {self.gate.consult_from_leg} or below "
                f"{self.gate.headroom_pct}% headroom, at most "
                f"{self.gate.max_per_poll} consults per poll, key "
                f"{('from ' + where) if found else 'NOT FOUND - every '
                   'consult will fail open and be counted'}")
        else:
            log("risk gate: disabled - the grid decides its own size")
        log(f"agent start: {len(live)} of {len(self.instruments)} "
            f"instrument(s) live [{', '.join(live)}], dry_run="
            f"{self.dry_run}")
        if dead:
            log(f"{len(dead)} instrument(s) HALTED AT STARTUP: "
                f"{', '.join(dead)}")
            self._reported_halted = list(dead)
        while True:
            clock = self.cli.clock()
            positions = self.positions_by_symbol()
            # Quotes are only worth a request while the market is open;
            # outside it every instrument returns before it looks at one.
            if clock["is_open"]:
                stock_quotes, option_quotes = self.market_data()
            else:
                stock_quotes, option_quotes = {}, {}
            # One extra request per poll, and only while the gate is on and
            # the market is open: margin is what the gate reasons about and
            # it is not in the position list.
            portfolio = None
            self.gate.start_poll()
            if self.gate.enabled and clock["is_open"]:
                portfolio = self.portfolio_view(self.cli.account(),
                                                stock_quotes)
            for inst in self.instruments:
                if inst.halted:
                    continue
                try:
                    inst.step(clock, positions, stock_quotes, option_quotes,
                              gate=self.gate, portfolio=portfolio)
                except Exception:                            # noqa: BLE001
                    inst.failures += 1
                    inst.say(f"POLL FAILED ({inst.failures}/{HALT_AFTER}) - "
                             f"this instrument did nothing this poll:\n"
                             f"{traceback.format_exc()}")
                    if inst.failures >= HALT_AFTER:
                        inst.halted = True
                        inst.say(f"HALTED after {HALT_AFTER} consecutive "
                                 f"failed polls. It will not trade again in "
                                 f"this run; its positions and state file "
                                 f"are untouched and need a human.")
                else:
                    inst.failures = 0
            halted = [i.name for i in self.instruments if i.halted]
            if halted and halted != self._reported_halted:
                log(f"{len(halted)} of {len(self.instruments)} instruments "
                    f"HALTED: {', '.join(halted)}")
                self._reported_halted = list(halted)
            if once:
                return
            time.sleep(self.poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="run exactly one decision pass and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print would-be orders (CLI --dry-run), mutate nothing")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)

    agent = KangarooAgent(config, dry_run=args.dry_run,
                          config_path=args.config)

    # One live agent per config, enforced by the OS. The watchdog that keeps
    # the run alive over several days works by starting the agent whenever
    # it looks dead - so the only thing standing between a false positive
    # and two processes writing the same state files is this lock. A dry run
    # touches no state and no account, so it is exempt.
    lock = None
    if not args.dry_run:
        lock = SingleInstance(
            os.path.join(os.path.dirname(os.path.abspath(args.config)),
                         "state", "agent.lock"))
        lock.acquire()
        log(f"instance lock held: {lock.path} (pid {os.getpid()})")
    try:
        agent.run(once=args.once)
    finally:
        if lock is not None:
            lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
