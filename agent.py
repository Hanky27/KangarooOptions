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

"""Kangaroo Options agent - drives the grid core against Alpaca paper trading.

Loop per poll (same ordering as the original tick loop: close check first,
and never open in the same iteration that closed a cluster):

  1. market clock - outside regular hours the agent only samples and waits
  2. underlying quote (bid/ask) - the ONLY input of the trigger math
  3. if the cluster holds legs: option bids -> cluster P&L -> take-profit
     check; on take-profit sell-to-close every leg, then Mode1 toggle
  4. otherwise / afterwards: rebuy check -> buy-to-open one new leg
     (nearest expiration inside the DTE window, strike nearest to spot)

Order handling: market orders, day time-in-force, unique client_order_ids
(kang_c<cluster>_l<leg>_<o|x>); fills are awaited by polling the order until
a terminal status (sleep is a sampling rate, not a timeout). Cancels - if
ever needed manually - are ID-based only.

Log lines that describe a market decision carry the timestamp of the QUOTE
they were decided on, not the wall clock.

Usage:
  python agent.py --config config.yaml [--once] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

import yaml

from alpaca_cli import AlpacaCli, AlpacaCliError
from kangaroo_core import KangarooCore

TERMINAL_BAD = {"canceled", "expired", "rejected", "done_for_day",
                "stopped", "suspended", "replaced"}


def log(message: str, data_ts: str | None = None) -> None:
    prefix = f"[{data_ts}] " if data_ts else ""
    print(f"{prefix}{message}", flush=True)


def atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


class KangarooAgent:
    def __init__(self, config: dict, dry_run: bool) -> None:
        self.cfg = config
        self.dry_run = dry_run
        self.underlying = config["underlying"]
        self.poll_seconds = float(config["poll_seconds"])
        self.poll_fill_seconds = float(config["poll_fill_seconds"])
        self.dte_min = int(config["dte_min"])
        self.dte_max = int(config["dte_max"])
        self.state_file = config["state_file"]
        self.cli = AlpacaCli(config["cli_path"], config.get("env_file"))
        self.core = KangarooCore(
            rebuy_1st_pct=config["rebuy_1st_pct"],
            rebuy_pct=config["rebuy_pct"],
            tp_pct=config["tp_pct"],
            initial_qty=config["initial_qty"],
            max_invest_count=config["max_invest_count"],
            start_long=config["start_long"],
        )
        self._market_was_open: bool | None = None

    # --- state persistence ----------------------------------------------

    def load_state(self) -> None:
        if not os.path.isfile(self.state_file):
            return
        with open(self.state_file, "r", encoding="utf-8") as fh:
            self.core.restore(json.load(fh))
        log(f"state restored: cluster {self.core.cluster_id} "
            f"{'CALL' if self.core.is_long else 'PUT'} "
            f"legs={self.core.invest_count}")

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        atomic_write_json(self.state_file, self.core.to_dict())

    def reconcile(self) -> None:
        """Every leg in the state must exist as a long position in the
        account with at least the leg's quantity - otherwise the persisted
        state and reality diverged, and the agent must not trade on it."""
        if not self.core.legs:
            return
        held = {p["symbol"]: float(p["qty"]) for p in self.cli.positions()}
        for leg in self.core.legs:
            if held.get(leg.option_symbol, 0.0) < leg.qty:
                raise AlpacaCliError(
                    f"state/positions mismatch: leg {leg.option_symbol} x{leg.qty} "
                    f"not (fully) present in the account - refusing to trade")

    # --- orders ----------------------------------------------------------

    def wait_filled(self, order_id: str) -> dict:
        """Poll one order until it is filled. A terminal non-filled status is
        an error. No timeout by design - the sleep is only a sampling rate."""
        while True:
            order = self.cli.order_get(order_id)
            status = order["status"]
            if status == "filled":
                if not order.get("filled_avg_price"):
                    raise AlpacaCliError(
                        f"order {order_id} is filled but carries no "
                        f"filled_avg_price: {order}")
                return order
            if status in TERMINAL_BAD:
                raise AlpacaCliError(f"order {order_id} ended as '{status}': {order}")
            time.sleep(self.poll_fill_seconds)

    def pick_contract(self, spot_mid: float) -> dict:
        """Nearest expiration inside [dte_min, dte_max], then the strike
        nearest to the current spot. Call for the long cluster, put for the
        short cluster. 'Today' comes from the market clock, not the wall
        clock of this machine."""
        right = "call" if self.core.is_long else "put"
        today = date.fromisoformat(self.cli.clock()["timestamp"][:10])
        contracts = self.cli.option_contracts(
            self.underlying,
            (today + timedelta(days=self.dte_min)).isoformat(),
            (today + timedelta(days=self.dte_max)).isoformat(),
            right,
        )
        tradable = [c for c in contracts if c["tradable"]]
        if not tradable:
            raise AlpacaCliError(
                f"no tradable {right} contracts for {self.underlying} in "
                f"DTE window [{self.dte_min}, {self.dte_max}]")
        nearest_exp = min(c["expiration_date"] for c in tradable)
        candidates = [c for c in tradable if c["expiration_date"] == nearest_exp]
        return min(candidates, key=lambda c: abs(float(c["strike_price"]) - spot_mid))

    def open_leg(self, qty: int, spot_bid: float, spot_ask: float,
                 quote_ts: str) -> None:
        contract = self.pick_contract((spot_bid + spot_ask) / 2.0)
        coid = f"kang_c{self.core.cluster_id}_l{self.core.invest_count}_o"
        if self.dry_run:
            body = self.cli.submit_option_market(
                contract["symbol"], qty, "buy", "buy_to_open", coid, dry_run=True)
            log(f"DRY-RUN would buy_to_open {qty}x {contract['symbol']}: {body}",
                quote_ts)
            return
        order = self.cli.submit_option_market(
            contract["symbol"], qty, "buy", "buy_to_open", coid)
        filled = self.wait_filled(order["id"])
        premium = float(filled["filled_avg_price"])
        self.core.add_leg(contract["symbol"], qty,
                          self.core.entry_reference(spot_bid, spot_ask), premium)
        self.save_state()
        log(f"OPEN leg {self.core.invest_count}/{self.core.max_invest_count} "
            f"cluster {self.core.cluster_id} "
            f"{'CALL' if self.core.is_long else 'PUT'}: "
            f"{qty}x {contract['symbol']} @ {premium} "
            f"(underlying {self.core.legs[-1].entry_underlying})", quote_ts)

    def close_cluster(self, profit_usd: float, quote_ts: str) -> None:
        if self.dry_run:
            log(f"DRY-RUN would close cluster {self.core.cluster_id} "
                f"({self.core.invest_count} legs, unrealized {profit_usd:.2f} USD)",
                quote_ts)
            return
        realized = 0.0
        for i, leg in enumerate(list(self.core.legs)):
            coid = f"kang_c{self.core.cluster_id}_l{i}_x"
            order = self.cli.submit_option_market(
                leg.option_symbol, leg.qty, "sell", "sell_to_close", coid)
            filled = self.wait_filled(order["id"])
            fill = float(filled["filled_avg_price"])
            realized += (fill - leg.entry_premium) * self.core.contract_multiplier * leg.qty
        was = "CALL" if self.core.is_long else "PUT"
        self.core.on_cluster_closed()
        self.save_state()
        log(f"CLOSE cluster {self.core.cluster_id - 1} ({was}): "
            f"realized {realized:.2f} USD - toggling to "
            f"{'CALL' if self.core.is_long else 'PUT'}", quote_ts)

    # --- main loop -------------------------------------------------------

    def step(self) -> None:
        clock = self.cli.clock()
        if not clock["is_open"]:
            if self._market_was_open is not False:
                log(f"market closed - next open {clock['next_open']} "
                    f"(clock {clock['timestamp']})")
                self._market_was_open = False
            return
        if self._market_was_open is not True:
            log(f"market open - next close {clock['next_close']} "
                f"(clock {clock['timestamp']})")
            self._market_was_open = True

        quote = self.cli.stock_quote(self.underlying)
        spot_bid, spot_ask, quote_ts = quote["bp"], quote["ap"], quote["t"]
        if not (spot_bid > 0 and spot_ask > 0):
            raise AlpacaCliError(f"invalid underlying quote: {quote}")

        if self.core.legs:
            bids = {}
            option_quotes = self.cli.option_quotes(
                [leg.option_symbol for leg in self.core.legs])
            for leg in self.core.legs:
                if leg.option_symbol not in option_quotes:
                    raise AlpacaCliError(
                        f"no quote for open leg {leg.option_symbol}")
                bids[leg.option_symbol] = option_quotes[leg.option_symbol]["bp"]
            profit = self.core.cluster_profit_usd(bids)
            if self.core.check_close(profit, spot_bid, spot_ask):
                self.close_cluster(profit, quote_ts)
                return  # never open in the same iteration as a close

        qty = self.core.check_rebuy(spot_bid, spot_ask)
        if qty:
            self.open_leg(qty, spot_bid, spot_ask, quote_ts)

    def run(self, once: bool) -> None:
        account = self.cli.account()
        log(f"account {account['account_number']} "
            f"options_approved_level={account['options_approved_level']} "
            f"options_trading_level={account['options_trading_level']}")
        self.load_state()
        self.reconcile()
        log(f"agent start: {self.underlying}, cluster {self.core.cluster_id} "
            f"{'CALL' if self.core.is_long else 'PUT'}, "
            f"legs={self.core.invest_count}, dry_run={self.dry_run}")
        while True:
            self.step()
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

    agent = KangarooAgent(config, dry_run=args.dry_run)
    agent.run(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
