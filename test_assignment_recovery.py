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

"""What an assignment does to a restart, measured on the live account.

On 2026-09-02 the broker assigned and exercised its way through eleven
option legs, leaving four stock positions behind: AMZN -100, GLD +100,
SLV +800, TLT +100. Two defects followed, one that had already fired and
one that was still waiting for the opening bell.

THE DEADLOCK, which had fired.
handle_expiries deliberately defers settling an expired leg while the
account still holds stock in that underlying - booking an intrinsic
settlement next to the assigned shares would count the same money twice.
reconcile, running straight after, then found the assigned leg missing
from the account and halted the instrument. And a halted instrument never
reaches step(), which is the ONLY place assignment_gate flattens the
stock that caused the deferral. GLD_long sat in that loop with GLD 100
shares in the account; no restart could ever have cleared it.

THE COLLISION, which had not fired yet.
assignment_gate reads the position snapshot the loader takes ONCE per
poll and shares with all 25 instruments. AMZN_long and AMZN_short both
see the same -100 AMZN row. Both buy 100. The account ends up +100 long a
stock nobody chose. Measured 2026-09-03 across every log the account has:
zero assign_flat orders ever - no assignment had yet met an open market -
so this was still unfired when four underlyings came to hold stock.
"""
from __future__ import annotations

from alpaca_cli import AlpacaCliError
from test_agent_sunk_pot import StubCli, make_agent, patch_settlement

GLD_SHORT = "GLD260902P00401000"
GLD_WING = GLD_SHORT + "W"


class FlatteningCli(StubCli):
    """StubCli that can also fill an equity market order."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.equity_orders: list[dict] = []

    def submit_equity_market(self, symbol, qty, side, coid):
        self.equity_orders.append({"symbol": symbol, "qty": qty,
                                   "side": side, "coid": coid})
        return {"id": f"eq-{len(self.equity_orders)}"}

    def order_get(self, order_id):
        return {"id": order_id, "status": "filled", "filled_avg_price": "405.10"}


def _gld_agent(cli):
    a = make_agent(cli, legs=[(GLD_SHORT, 1, 1.20, 401.0, 396.0, "2026-09-02")])
    a.underlying = "GLD"
    return a


def test_a_leg_the_broker_settled_is_not_a_mismatch():
    """The exact state GLD_long was stuck in: leg assigned, stock held."""
    cli = StubCli(is_open=False, timestamp="2026-09-03T05:00:00-04:00",
                  positions=[{"symbol": "GLD", "qty": "100", "side": "long"}])
    a = _gld_agent(cli)
    positions = {p["symbol"]: p for p in cli.positions()}

    # Without the broker's record this must still abort - a missing leg
    # that nobody settled is exactly what reconcile exists to catch.
    try:
        a.reconcile(positions, set())
    except AlpacaCliError as exc:
        assert "not present as short" in str(exc), exc
    else:
        raise AssertionError("a leg missing for no reason must still abort")

    # With it, and with the stock that proves the deferral is live, the
    # instrument stays alive so step() can flatten and settle it.
    a.reconcile(positions, {GLD_SHORT, GLD_WING})


def test_the_exemption_needs_the_stock_as_well_as_the_record():
    """No stock held means handle_expiries had no reason to defer."""
    cli = StubCli(is_open=False, timestamp="2026-09-03T05:00:00-04:00")
    a = _gld_agent(cli)
    try:
        a.reconcile({}, {GLD_SHORT, GLD_WING})
    except AlpacaCliError as exc:
        assert "not present as short" in str(exc), exc
    else:
        raise AssertionError(
            "without the stock position the leg is simply gone, and that "
            "still has to stop the instrument")


def test_flattening_removes_the_row_the_other_direction_would_reuse():
    """One shared snapshot, two instruments, one flatten."""
    cli = FlatteningCli(is_open=True, timestamp="2026-09-03T10:00:00-04:00",
                        positions=[{"symbol": "AMZN", "qty": "-100",
                                    "side": "short"}])
    positions = {p["symbol"]: p for p in cli.positions()}
    long_side = make_agent(cli, legs=[])
    long_side.underlying = "AMZN"
    short_side = make_agent(cli, legs=[])
    short_side.underlying = "AMZN"

    long_side.assignment_gate(positions)
    short_side.assignment_gate(positions)

    assert len(cli.equity_orders) == 1, cli.equity_orders
    assert cli.equity_orders[0] == {"symbol": "AMZN", "qty": 100.0,
                                    "side": "buy",
                                    "coid": "kang_assign_flat_AMZN"}
    assert "AMZN" not in positions


def test_the_flatten_unblocks_the_settlement_in_the_same_pass():
    """handle_expiries defers on the snapshot assignment_gate just fixed."""
    patch_settlement()
    cli = FlatteningCli(is_open=True, timestamp="2026-09-03T10:00:00-04:00",
                        positions=[{"symbol": "SPY", "qty": "200",
                                    "side": "long"}])
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    positions = {p["symbol"]: p for p in cli.positions()}

    a.handle_expiries("2026-09-05", positions)
    assert len(a.core.legs) == 1, "deferred while the stock is still there"

    a.assignment_gate(positions)
    a.handle_expiries("2026-09-05", positions)
    assert a.core.legs == [], "and settled once it is gone"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
