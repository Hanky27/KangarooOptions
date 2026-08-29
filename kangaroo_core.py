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

"""Kangaroo Options core - martingale-style grid state machine, options edition.

Stripped-down port of the author's cTrader Kangaroo V2.2 grid logic for the
Alpaca AI Trading Agents Hackathon. Deliberately reduced to ONE purpose
(variant A):

  - Mode1 only: when a cluster closes in profit, direction toggles
    (original: Instance.cs `IsLong = !IsLong`).
  - Long cluster  -> legs are LONG CALLS (buy_to_open)
  - Short cluster -> legs are LONG PUTS  (buy_to_open)

Everything else from the original is intentionally NOT here: Mode2/Mode3,
Freeze/Unfreeze, Hedging/Netting order modes, grid close, PID factors,
CloseOnly, multi-symbol handling, FX pip/spread simulation.

Both cluster directions consist exclusively of buy-to-open orders; direction
is expressed by the contract type (call vs put), never by short selling.

Trigger math runs on the UNDERLYING quote (bid/ask), never on option
premiums. The take-profit check runs on the aggregated option P&L in USD.
This module is pure state + math: no I/O, no broker calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Growth factor of leg sizes, as in the original
# (kangaroo.py: initial_volume * 1.1 ** invest_count).
VOLUME_GROWTH = 1.1


def settlement_pnl(kind: str, right: str, strike: float,
                   wing_strike: float | None, entry_premium: float,
                   qty: int, underlying_close: float,
                   contract_multiplier: int = 100) -> tuple[float, bool]:
    """Realized USD of ONE leg that expires, plus whether it expired ITM.

    Pure math, no I/O - the ONE place both the simulator and the live agent
    settle an expiring leg, so a live cluster's take-profit decision cannot
    drift from the backtested one.

    kind is "long_option", "short_put", "call_spread" or "put_spread";
    right ("call"/"put") is only read for "long_option". A spread settles at
    its NET intrinsic, which is why its loss is capped at the wing distance.
    """
    if kind == "long_option":
        intrinsic = (max(underlying_close - strike, 0.0) if right == "call"
                     else max(strike - underlying_close, 0.0))
        pnl = (intrinsic - entry_premium) * contract_multiplier * qty
        return pnl, intrinsic > 0
    if kind == "short_put":
        intrinsic = max(strike - underlying_close, 0.0)
        pnl = (entry_premium - intrinsic) * contract_multiplier * qty
        return pnl, intrinsic > 0
    if wing_strike is None:
        raise ValueError(f"{kind} needs a wing_strike")
    if kind == "call_spread":
        net_intrinsic = (max(underlying_close - strike, 0.0)
                         - max(underlying_close - wing_strike, 0.0))
    elif kind == "put_spread":
        net_intrinsic = (max(strike - underlying_close, 0.0)
                         - max(wing_strike - underlying_close, 0.0))
    else:
        raise ValueError(f"unknown leg kind {kind!r}")
    pnl = (entry_premium - net_intrinsic) * contract_multiplier * qty
    return pnl, net_intrinsic > 0


@dataclass
class Leg:
    """One grid leg = one long option position (call or put per cluster side)."""
    option_symbol: str        # OCC symbol, e.g. SPY260904C00780000
    qty: int                  # contracts (integer, unlike FX lots)
    entry_underlying: float   # underlying reference price at entry (trigger base)
    entry_premium: float      # filled average premium per share (USD)


class KangarooCore:
    """Cluster state machine. The caller (agent) owns all I/O and ordering:
    it must run the close check BEFORE the rebuy check on every poll, and
    must not open a new leg in the same poll iteration that closed a cluster
    (same tick ordering as the original bar/tick loop)."""

    def __init__(
        self,
        rebuy_1st_pct: float,
        rebuy_pct: float,
        tp_pct: float,
        initial_qty: int,
        max_invest_count: int,
        start_long: bool = True,
        contract_multiplier: int = 100,
    ) -> None:
        if rebuy_1st_pct <= 0 or rebuy_pct <= 0 or tp_pct <= 0:
            raise ValueError("rebuy_1st_pct, rebuy_pct and tp_pct must be > 0")
        if initial_qty < 1 or max_invest_count < 1:
            raise ValueError("initial_qty and max_invest_count must be >= 1")
        self.rebuy_1st_pct = float(rebuy_1st_pct)
        self.rebuy_pct = float(rebuy_pct)
        self.tp_pct = float(tp_pct)
        self.initial_qty = int(initial_qty)
        self.max_invest_count = int(max_invest_count)
        self.contract_multiplier = int(contract_multiplier)
        self.is_long = bool(start_long)   # True: call cluster, False: put cluster
        self.cluster_id = 1
        self.legs: list[Leg] = []
        # Realized USD of legs that already left the cluster (expiry
        # settlement, execution cost). The original has no such legs - FX
        # positions never expire - but its ClusterProfit is likewise
        # "open positions + already closed ones" (Instance.cs:442), so the
        # take-profit test runs on the same total here.
        self.sunk_pot = 0.0

    # --- state -----------------------------------------------------------

    @property
    def invest_count(self) -> int:
        return len(self.legs)

    def next_leg_qty(self) -> int:
        """Contracts for the next leg: initial_qty * 1.1^invest_count,
        rounded to whole contracts, never below 1."""
        return max(1, int(round(self.initial_qty * VOLUME_GROWTH ** self.invest_count)))

    def entry_reference(self, spot_bid: float, spot_ask: float) -> float:
        """Underlying reference price stored with a new leg. Call cluster
        references the ask (a long exposure 'pays' the ask), put cluster the
        bid - same sides as the original leg_entry_fill on the underlying."""
        return spot_ask if self.is_long else spot_bid

    def add_leg(self, option_symbol: str, qty: int,
                entry_underlying: float, entry_premium: float) -> None:
        if qty < 1:
            raise ValueError("leg qty must be >= 1")
        self.legs.append(Leg(option_symbol, int(qty),
                             float(entry_underlying), float(entry_premium)))

    # --- decisions -------------------------------------------------------

    def check_rebuy(self, spot_bid: float, spot_ask: float) -> int:
        """Return number of contracts to buy now, 0 = no action.

        Empty cluster -> initial open (pure grid, no entry signal).
        Otherwise: rebuy when the underlying moved adversely by
        rebuy_1st_pct (while invest_count < 2) or rebuy_pct (afterwards),
        measured from the LAST leg's entry reference price.
        Adverse for a call cluster = underlying fell; for a put cluster =
        underlying rose."""
        if not self.legs:
            return self.initial_qty
        if self.invest_count >= self.max_invest_count:
            return 0
        ref = self.legs[-1].entry_underlying
        pct = self.rebuy_1st_pct if self.invest_count < 2 else self.rebuy_pct
        if self.is_long:
            triggered = spot_ask < ref * (1.0 - pct / 100.0)
        else:
            triggered = spot_bid > ref * (1.0 + pct / 100.0)
        return self.next_leg_qty() if triggered else 0

    def cluster_profit_usd(self, option_bids: dict[str, float]) -> float:
        """Aggregated unrealized P&L of the cluster in USD. All legs are long
        options, so the exit side is always the option BID (sell to close).
        Raises KeyError if a leg has no quote - the caller must fail loud."""
        total = 0.0
        for leg in self.legs:
            bid = option_bids[leg.option_symbol]
            total += (bid - leg.entry_premium) * self.contract_multiplier * leg.qty
        return total

    def check_close(self, open_pnl_usd: float,
                    spot_bid: float, spot_ask: float) -> bool:
        """Take-profit check, same shape as the original:
        close when ClusterProfit > InvestCount * InitialTargetCash with
        InitialTargetCash = initial_qty * multiplier * (tp_pct% of the
        underlying exit-side price). The multiplier maps '1 contract' to
        '100 underlying units', the option-world analogue of one FX lot unit.

        ClusterProfit is the WHOLE cluster - the open legs' P&L plus
        sunk_pot, mirroring `HedgePositions.Sum(Profit) + mClosedProfit`
        (Instance.cs:442) tested at Instance.cs:618. Since the threshold is
        positive, a cluster can never take profit while its total is
        negative: a loss on already-settled legs must be earned back first.

        Note: long options carry delta < 1, so reaching the threshold needs
        more underlying movement than in the FX original - tp_pct is a
        tuning parameter, not a like-for-like carryover."""
        if not self.legs:
            return False
        spot_exit = spot_bid if self.is_long else spot_ask
        tp_value = spot_exit * self.tp_pct / 100.0
        threshold = self.invest_count * self.initial_qty * self.contract_multiplier * tp_value
        return open_pnl_usd + self.sunk_pot > threshold

    def book_settled(self, amount_usd: float) -> None:
        """Add realized USD of a leg that left the cluster (expiry
        settlement, execution cost) to the cluster's sunk pot."""
        self.sunk_pot += float(amount_usd)

    def on_cluster_closed(self, toggle: bool = True) -> None:
        """End the current cluster. toggle=True is the original Mode1
        behavior (Instance.cs `IsLong = !IsLong`); the put-only agent passes
        toggle=False and keeps its direction (backtests 2026-08-28: the
        Mode1 short side lost money in every measured style)."""
        self.legs.clear()
        self.sunk_pot = 0.0
        if toggle:
            self.is_long = not self.is_long
        self.cluster_id += 1

    # --- persistence -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "is_long": self.is_long,
            "cluster_id": self.cluster_id,
            "sunk_pot": self.sunk_pot,
            "legs": [
                {
                    "option_symbol": leg.option_symbol,
                    "qty": leg.qty,
                    "entry_underlying": leg.entry_underlying,
                    "entry_premium": leg.entry_premium,
                }
                for leg in self.legs
            ],
        }

    def restore(self, state: dict) -> None:
        """Restore cluster state from a to_dict() snapshot. Unknown or
        missing keys are an error - never guess a trading state."""
        self.is_long = bool(state["is_long"])
        self.cluster_id = int(state["cluster_id"])
        self.sunk_pot = float(state["sunk_pot"])
        self.legs = [
            Leg(
                option_symbol=str(leg["option_symbol"]),
                qty=int(leg["qty"]),
                entry_underlying=float(leg["entry_underlying"]),
                entry_premium=float(leg["entry_premium"]),
            )
            for leg in state["legs"]
        ]
