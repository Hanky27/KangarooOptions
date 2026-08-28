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

"""Kangaroo Options agent - put-credit-spread grid on Alpaca paper trading.

Configuration chosen from the 11-run backtest sweep of 2026-08-28 (SPY,
2024-02..2026-08, real Alpaca option prices): the PUT-ONLY grid selling PUT
CREDIT SPREADS was the only account-sized profitable variant (+34,484 USD,
max drawdown -2,721 USD, margin peak 2,000 USD); every long-option variant
was negative and the Mode1 short side lost money in every measured style.

Strategy per leg: SELL the ATM put, BUY a protective put ~5 $ lower (width
probed over configured candidates) - one mleg LIMIT order at a marketable
net-credit limit (short bid - wing ask). The grid logic is unchanged
Kangaroo: rebuy on adverse (falling) underlying moves with 1.1^n growth,
take-profit on the cluster's aggregated closeable P&L, direction never
toggles (put-only).

Loop per poll (close check first, never open in the same iteration):
  0. expiry handling - legs whose expiration has passed are dropped (the
     broker settles them; user rule 2026-08-28: let legs expire), and an
     ASSIGNED underlying stock position is flattened immediately
  1. market clock gate (outside regular hours: sample and wait)
  2. underlying quote -> trigger math (never option premiums)
  3. take-profit check on the OPEN legs' closeable P&L (buy back short at
     ask, sell wing at bid); on TP close every spread, then restart the
     grid in the same direction
  4. rebuy check -> sell one new put credit spread

Orders: mleg LIMIT day orders (order form verified on the paper account
2026-08-28). An order that does not fill within fill_requote_samples polls
is canceled BY ID and re-quoted on the next loop - never left dangling,
never cancel-all. Log lines describing market decisions carry the QUOTE
timestamp, not the wall clock.

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
        self.fill_requote_samples = int(config["fill_requote_samples"])
        self.dte_min = int(config["dte_min"])
        self.dte_max = int(config["dte_max"])
        self.spread_widths = [int(w) for w in config["spread_widths"]]
        self.state_file = config["state_file"]
        self.cli = AlpacaCli(config["cli_path"], config.get("env_file"))
        self.core = KangarooCore(
            rebuy_1st_pct=config["rebuy_1st_pct"],
            rebuy_pct=config["rebuy_pct"],
            tp_pct=config["tp_pct"],
            initial_qty=config["initial_qty"],
            max_invest_count=config["max_invest_count"],
            start_long=True,          # put-only: the direction never changes
        )
        # per-leg spread details, keyed by the SHORT leg's OCC symbol
        self.leg_extras: dict[str, dict] = {}
        self._market_was_open: bool | None = None

    # --- state persistence ----------------------------------------------

    def load_state(self) -> None:
        if not os.path.isfile(self.state_file):
            return
        with open(self.state_file, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        self.core.restore(payload["core"])
        self.leg_extras = payload["leg_extras"]
        for leg in self.core.legs:
            if leg.option_symbol not in self.leg_extras:
                raise AlpacaCliError(
                    f"state file has no spread details for {leg.option_symbol}")
        log(f"state restored: cluster {self.core.cluster_id} "
            f"legs={self.core.invest_count}")

    def save_state(self) -> None:
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        extras = {leg.option_symbol: self.leg_extras[leg.option_symbol]
                  for leg in self.core.legs}
        atomic_write_json(self.state_file,
                          {"core": self.core.to_dict(), "leg_extras": extras})

    def reconcile(self) -> None:
        """Every spread leg in the state must exist in the account: the
        short put as a short position, the wing as a long position, each
        with at least the leg's quantity. Any mismatch aborts."""
        if not self.core.legs:
            return
        positions = {p["symbol"]: p for p in self.cli.positions()}

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
                    f"state/positions mismatch: short leg {leg.option_symbol} "
                    f"x{leg.qty} not present as short - refusing to trade")
            if not held(extra["wing_occ"], False, leg.qty):
                raise AlpacaCliError(
                    f"state/positions mismatch: wing {extra['wing_occ']} "
                    f"x{leg.qty} not present as long - refusing to trade")

    # --- order helpers ---------------------------------------------------

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
                log(f"order {order_id} canceled unfilled - will re-quote")
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

    def select_spread(self, spot_mid: float) -> dict:
        """Nearest expiration inside [dte_min, dte_max]; short strike
        nearest to spot; wing = first width candidate that exists as a
        strike at that expiration. Prices come from live quotes."""
        today = date.fromisoformat(self.cli.clock()["timestamp"][:10])
        contracts = self.cli.option_contracts(
            self.underlying,
            (today + timedelta(days=self.dte_min)).isoformat(),
            (today + timedelta(days=self.dte_max)).isoformat(),
            "put",
        )
        tradable = [c for c in contracts if c["tradable"]]
        if not tradable:
            raise AlpacaCliError(
                f"no tradable puts for {self.underlying} in "
                f"DTE [{self.dte_min},{self.dte_max}]")
        expiry = min(c["expiration_date"] for c in tradable)
        chain = {float(c["strike_price"]): c
                 for c in tradable if c["expiration_date"] == expiry}
        short_strike = min(chain, key=lambda s: abs(s - spot_mid))
        wing_strike = None
        for width in self.spread_widths:
            if short_strike - width in chain:
                wing_strike = short_strike - width
                break
        if wing_strike is None:
            raise AlpacaCliError(
                f"no wing strike below {short_strike} (widths "
                f"{self.spread_widths}) at {expiry}")
        occ_s = chain[short_strike]["symbol"]
        occ_w = chain[wing_strike]["symbol"]
        quotes = self.cli.option_quotes([occ_s, occ_w])
        for occ in (occ_s, occ_w):
            if occ not in quotes:
                raise AlpacaCliError(f"no quote for {occ}")
        credit_limit = quotes[occ_s]["bp"] - quotes[occ_w]["ap"]
        if credit_limit <= 0:
            raise AlpacaCliError(
                f"non-positive marketable credit {credit_limit} for "
                f"{occ_s}/{occ_w} - refusing to sell")
        return {
            "occ_short": occ_s, "occ_wing": occ_w,
            "strike": short_strike, "wing_strike": wing_strike,
            "expiry": expiry, "credit_limit": round(credit_limit, 2),
            "quote_ts": quotes[occ_s]["t"],
        }

    # --- trading actions -------------------------------------------------

    def open_leg(self, qty: int, spot_bid: float, spot_ask: float) -> None:
        spread = self.select_spread((spot_bid + spot_ask) / 2.0)
        coid = f"kang_c{self.core.cluster_id}_l{self.core.invest_count}_o"
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
            log(f"DRY-RUN would sell {qty}x put credit spread "
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
        log(f"OPEN leg {self.core.invest_count}/{self.core.max_invest_count} "
            f"cluster {self.core.cluster_id}: sold {qty}x "
            f"{spread['occ_short']}/{spread['occ_wing']} "
            f"credit {net_credit:.2f} (underlying "
            f"{self.core.legs[-1].entry_underlying})", spread["quote_ts"])

    def closeable_pnl(self) -> tuple[float, dict, str]:
        """(cluster P&L closeable NOW, per-leg net close costs, quote ts).
        Close cost per spread = short ask - wing bid."""
        symbols = []
        for leg in self.core.legs:
            symbols += [leg.option_symbol,
                        self.leg_extras[leg.option_symbol]["wing_occ"]]
        quotes = self.cli.option_quotes(symbols)
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
            log(f"DRY-RUN would close cluster {self.core.cluster_id} "
                f"({self.core.invest_count} legs)", quote_ts)
            return
        realized = 0.0
        remaining = list(self.core.legs)
        for i, leg in enumerate(remaining):
            extra = self.leg_extras[leg.option_symbol]
            coid = f"kang_c{self.core.cluster_id}_l{i}_x"
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
                log(f"close of {leg.option_symbol} did not fill - "
                    f"keeping remaining legs, re-quoting next loop", quote_ts)
                self.save_state()
                return
            fill_s, fill_w = self._leg_fills(filled, leg.option_symbol,
                                             extra["wing_occ"])
            realized += ((leg.entry_premium - (fill_s - fill_w))
                         * self.core.contract_multiplier * leg.qty)
            self.core.legs.remove(leg)
            self.save_state()
        ended = self.core.cluster_id
        self.core.on_cluster_closed(toggle=False)   # put-only: no Mode1
        self.save_state()
        log(f"CLOSE cluster {ended}: realized {realized:.2f} USD "
            f"- restarting put grid (cluster {self.core.cluster_id})",
            quote_ts)

    # --- gates -----------------------------------------------------------

    def handle_expiries(self, clock_date: str) -> None:
        """User rule: let legs expire. A leg whose expiration date has
        passed was settled by the broker overnight - drop it here."""
        expired = [l for l in self.core.legs
                   if clock_date > self.leg_extras[l.option_symbol]["expiry"]]
        if not expired:
            return
        for leg in expired:
            log(f"EXPIRED leg {leg.option_symbol} x{leg.qty} "
                f"(settled by broker) - dropping from cluster")
            self.core.legs.remove(leg)
        if not self.core.legs:
            ended = self.core.cluster_id
            self.core.on_cluster_closed(toggle=False)
            log(f"cluster {ended} fully expired - restarting put grid "
                f"(cluster {self.core.cluster_id})")
        self.save_state()

    def assignment_gate(self) -> None:
        """An assigned stock position is flattened immediately (user rule
        from the reference options bot: no wheel, close assignments)."""
        for p in self.cli.positions():
            if p["symbol"] != self.underlying:
                continue
            signed = float(p["qty"])
            side = "sell" if signed > 0 else "buy"
            log(f"ASSIGNMENT GATE: found {signed} {self.underlying} - "
                f"flattening at market")
            if self.dry_run:
                return
            order = self.cli.submit_equity_market(
                self.underlying, abs(signed), side,
                f"kang_assign_flat_{p['symbol']}")
            while True:                 # market order: await the fill
                o = self.cli.order_get(order["id"])
                if o["status"] == "filled":
                    log(f"assignment flattened: {o.get('filled_avg_price')}")
                    return
                if o["status"] in TERMINAL_BAD:
                    raise AlpacaCliError(f"assignment flatten failed: {o}")
                time.sleep(self.poll_fill_seconds)

    # --- main loop -------------------------------------------------------

    def step(self) -> None:
        clock = self.cli.clock()
        self.handle_expiries(clock["timestamp"][:10])
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
        self.assignment_gate()

        quote = self.cli.stock_quote(self.underlying)
        spot_bid, spot_ask, quote_ts = quote["bp"], quote["ap"], quote["t"]
        if not (spot_bid > 0 and spot_ask > 0):
            raise AlpacaCliError(f"invalid underlying quote: {quote}")

        if self.core.legs:
            pnl, costs, opt_ts = self.closeable_pnl()
            if self.core.check_close(pnl, spot_bid, spot_ask):
                self.close_cluster(costs, opt_ts)
                return  # never open in the same iteration as a close

        qty = self.core.check_rebuy(spot_bid, spot_ask)
        if qty:
            self.open_leg(qty, spot_bid, spot_ask)

    def run(self, once: bool) -> None:
        account = self.cli.account()
        log(f"account {account['account_number']} "
            f"options_approved_level={account['options_approved_level']} "
            f"options_trading_level={account['options_trading_level']}")
        self.load_state()
        self.reconcile()
        log(f"agent start: put-credit-spread grid on {self.underlying}, "
            f"cluster {self.core.cluster_id}, legs={self.core.invest_count}, "
            f"dry_run={self.dry_run}")
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
