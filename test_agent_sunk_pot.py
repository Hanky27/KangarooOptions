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

"""Expiry accounting of the live agent, driven with a stub broker.

The agent's take-profit decision must be the one the simulator would make
on the same cluster, so every leg that leaves the cluster has to reach the
sunk pot. These tests drive KangarooAgent against a fake CLI - no network,
no orders - and check the pot after each event.

Run: python test_agent_sunk_pot.py
"""

import os
import sys
import tempfile
from datetime import date

import agent as agent_mod
from agent import Instrument, load_instrument_configs
from kangaroo_core import settlement_pnl

SETTLE_CLOSE = 767.00      # underlying close of the expiry day


class StubCli:
    """Minimal stand-in for AlpacaCli: records calls, returns fixtures."""

    def __init__(self, *, is_open: bool, timestamp: str, positions=None):
        self._is_open = is_open
        self._timestamp = timestamp
        self._positions = positions or []
        self.calls: list[str] = []

    def clock(self):
        self.calls.append("clock")
        return {"timestamp": self._timestamp, "is_open": self._is_open,
                "next_open": "-", "next_close": "-"}

    def positions(self):
        self.calls.append("positions")
        return list(self._positions)

    def account(self):
        self.calls.append("account")
        return {"account_number": "STUB", "options_approved_level": 3,
                "options_trading_level": 3}


def make_agent(cli, *, legs, sunk_pot=0.0):
    """An agent with a hand-built cluster and no I/O beyond the stub."""
    state_dir = tempfile.mkdtemp(prefix="kang_test_")
    cfg = {
        "underlying": "SPY", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
        "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
        "dte_min": 4, "dte_max": 10, "spread_widths": [5],
        "poll_seconds": 5, "poll_fill_seconds": 2,
        "fill_requote_samples": 30,
        "state_file": os.path.join(state_dir, "state.json"),
        "cli_path": "unused", "env_file": None,
    }
    cfg["max_adverse_pct"] = 0.0
    a = Instrument(cfg, cli, dry_run=False, config_dir=state_dir)
    a.core.sunk_pot = sunk_pot
    a.leg_extras = {}
    for occ, qty, credit, strike, wing, expiry in legs:
        a.core.add_leg(occ, qty, 770.0, credit)
        a.leg_extras[occ] = {"wing_occ": occ + "W", "strike": strike,
                             "wing_strike": wing, "expiry": expiry}
    return a


def patch_settlement(monkey_close=SETTLE_CLOSE):
    """Replace the network fetch with a fixed bar."""
    agent_mod.fetch_bars = lambda sym, tf, start, end: (
        [{"c": monkey_close, "t": start}] if monkey_close else [])


def test_loader_without_config_path_yields_one_instrument():
    loader = {"underlying": "SPY", "config_path": "_", "tp_pct": 0.10}
    got = load_instrument_configs(loader, ".")
    assert len(got) == 1 and got[0]["underlying"] == "SPY", got


def test_loader_reads_one_config_per_instrument_and_inherits():
    folder = tempfile.mkdtemp(prefix="kang_cfgs_")
    with open(os.path.join(folder, "spy.yaml"), "w", encoding="utf-8") as fh:
        fh.write("underlying: SPY\ntp_pct: 0.04\n")
    with open(os.path.join(folder, "qqq.yaml"), "w", encoding="utf-8") as fh:
        fh.write("underlying: QQQ\n")
    loader = {"underlying": "IGNORED", "tp_pct": 0.10, "dte_min": 4,
              "config_path": folder}
    got = load_instrument_configs(loader, ".")
    by = {c["underlying"]: c for c in got}
    assert set(by) == {"SPY", "QQQ"}, by
    assert by["SPY"]["tp_pct"] == 0.04, "own value wins"
    assert by["QQQ"]["tp_pct"] == 0.10, "loader value is inherited"
    assert by["QQQ"]["dte_min"] == 4, "loader value is inherited"


def test_loader_refuses_a_duplicate_underlying():
    folder = tempfile.mkdtemp(prefix="kang_dup_")
    for name in ("a.yaml", "b.yaml"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write("underlying: SPY\n")
    try:
        load_instrument_configs({"config_path": folder}, ".")
    except agent_mod.AlpacaCliError as exc:
        assert "same underlying" in str(exc), exc
    else:
        raise AssertionError("two grids on one symbol must fail loud")


def test_instrument_config_may_not_override_process_keys():
    folder = tempfile.mkdtemp(prefix="kang_proc_")
    with open(os.path.join(folder, "spy.yaml"), "w", encoding="utf-8") as fh:
        fh.write("underlying: SPY\ncli_path: /somewhere/else\n")
    try:
        load_instrument_configs({"config_path": folder}, ".")
    except agent_mod.AlpacaCliError as exc:
        assert "loader-only" in str(exc), exc
    else:
        raise AssertionError("a per-instrument config must not move the CLI")


def test_state_file_is_absolute_and_per_symbol():
    d = tempfile.mkdtemp(prefix="kang_state_")
    cfg = {"underlying": "QQQ", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 4, "dte_max": 10,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30}
    inst = Instrument(cfg, None, dry_run=True, config_dir=d)
    assert os.path.isabs(inst.state_file), inst.state_file
    assert "qqq" in os.path.basename(inst.state_file), inst.state_file
    other = Instrument(dict(cfg, underlying="SPY"), None, True, d)
    assert inst.state_file != other.state_file


def test_client_order_id_carries_the_instrument():
    cfg = {"underlying": "AMD", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 4, "dte_max": 10,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30}
    a = Instrument(cfg, None, True, tempfile.mkdtemp())
    b = Instrument(dict(cfg, underlying="MSFT"), None, True,
                   tempfile.mkdtemp())
    assert a.coid("o", 0) != b.coid("o", 0), "ids would collide in one account"
    assert "AMD" in a.coid("o", 0) and "MSFT" in b.coid("o", 0)


class ChainCli:
    """Stub CLI holding one expiry with a whole-dollar strike grid."""

    def __init__(self, right):
        self.right = right
        self.asked_right = None

    def option_contracts(self, underlying, gte, lte, right):
        self.asked_right = right
        return [{"symbol": f"{underlying}260904{right[0].upper()}{k:05d}000",
                 "strike_price": float(k), "expiration_date": "2026-09-04",
                 "tradable": True} for k in range(760, 781)]

    def option_quotes(self, occs):
        # short leg richer than the wing, so the credit is positive
        return {occ: {"bp": 2.00 - i, "ap": 2.10 - i, "t": "2026-08-31T13:35"}
                for i, occ in enumerate(occs)}


def _side_instrument(start_long, cli=None):
    cfg = {"underlying": "SPY", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 4, "dte_max": 10,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30, "start_long": start_long}
    return Instrument(cfg, cli, True, tempfile.mkdtemp(prefix="kang_side_"))


def test_one_symbol_may_carry_both_directions():
    folder = tempfile.mkdtemp(prefix="kang_both_")
    with open(os.path.join(folder, "spy_long.yaml"), "w",
              encoding="utf-8") as fh:
        fh.write("underlying: SPY\nstart_long: true\n")
    with open(os.path.join(folder, "spy_short.yaml"), "w",
              encoding="utf-8") as fh:
        fh.write("underlying: SPY\nstart_long: false\n")
    got = load_instrument_configs({"config_path": folder, "tp_pct": 0.04}, ".")
    assert len(got) == 2, got
    assert {c["start_long"] for c in got} == {True, False}, got


def test_two_grids_on_one_side_of_one_symbol_still_fail():
    folder = tempfile.mkdtemp(prefix="kang_samesde_")
    for name in ("a.yaml", "b.yaml"):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write("underlying: SPY\nstart_long: true\n")
    try:
        load_instrument_configs({"config_path": folder}, ".")
    except agent_mod.AlpacaCliError as exc:
        assert "direction" in str(exc), exc
    else:
        raise AssertionError("two long grids on SPY must fail loud")


def test_loader_may_not_hand_one_state_file_to_every_instrument():
    folder = tempfile.mkdtemp(prefix="kang_share_")
    with open(os.path.join(folder, "spy.yaml"), "w", encoding="utf-8") as fh:
        fh.write("underlying: SPY\n")
    loader = {"config_path": folder, "state_file": "state/one.json"}
    try:
        load_instrument_configs(loader, ".")
    except agent_mod.AlpacaCliError as exc:
        assert "shared by every" in str(exc), exc
    else:
        raise AssertionError("one state file for N grids must fail loud")


def test_an_instrument_may_still_name_its_own_state_file():
    folder = tempfile.mkdtemp(prefix="kang_own_")
    with open(os.path.join(folder, "spy.yaml"), "w", encoding="utf-8") as fh:
        fh.write("underlying: SPY\nstate_file: state/mine.json\n")
    got = load_instrument_configs({"config_path": folder}, ".")
    assert got[0]["state_file"] == "state/mine.json", got


def test_the_two_directions_do_not_share_state_or_order_ids():
    lo, sh = _side_instrument(True), _side_instrument(False)
    assert lo.state_file != sh.state_file, lo.state_file
    assert "long" in lo.state_file and "short" in sh.state_file
    assert lo.coid("open", 0) != sh.coid("open", 0)


def test_long_grid_sells_puts_with_the_wing_below():
    cli = ChainCli("put")
    inst = _side_instrument(True, cli)
    spread = inst.select_spread(770.0, date(2026, 8, 31))
    assert cli.asked_right == "put", cli.asked_right
    assert spread["strike"] == 770.0, spread
    assert spread["wing_strike"] == 765.0, spread
    assert spread["credit_limit"] > 0, spread


def test_short_grid_sells_calls_with_the_wing_above():
    cli = ChainCli("call")
    inst = _side_instrument(False, cli)
    spread = inst.select_spread(770.0, date(2026, 8, 31))
    assert cli.asked_right == "call", cli.asked_right
    assert spread["strike"] == 770.0, spread
    assert spread["wing_strike"] == 775.0, spread
    assert spread["credit_limit"] > 0, spread


def test_expired_leg_reaches_the_pot():
    patch_settlement()
    cli = StubCli(is_open=False, timestamp="2026-09-05T00:00:03-04:00")
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    a.handle_expiries("2026-09-05", {p['symbol']: p for p in cli.positions()})
    expected, itm = settlement_pnl("put_spread", "put", 770.0, 765.0,
                                   1.20, 2, SETTLE_CLOSE, 100)
    assert itm is True, "770 short put against a 767 close is ITM"
    # the cluster ended (no legs left), so the pot was booked and reset -
    # what must hold is that the leg is gone and nothing was invented
    assert a.core.legs == [], a.core.legs
    assert a.core.sunk_pot == 0.0, a.core.sunk_pot
    assert a.core.cluster_id == 2, a.core.cluster_id
    # and the amount the core would have carried is the shared formula's
    assert expected == (1.20 - 3.0) * 100 * 2, expected


def test_pot_survives_while_other_legs_stay_open():
    patch_settlement()
    cli = StubCli(is_open=False, timestamp="2026-09-05T00:00:03-04:00")
    a = make_agent(cli, legs=[
        ("SPY260904P00770000", 2, 1.20, 770.0, 765.0, "2026-09-04"),
        ("SPY260911P00760000", 1, 1.00, 760.0, 755.0, "2026-09-11"),
    ])
    a.handle_expiries("2026-09-05", {p['symbol']: p for p in cli.positions()})
    expected, _ = settlement_pnl("put_spread", "put", 770.0, 765.0,
                                 1.20, 2, SETTLE_CLOSE, 100)
    assert len(a.core.legs) == 1, a.core.legs
    assert abs(a.core.sunk_pot - expected) < 1e-9, a.core.sunk_pot
    assert a.core.cluster_id == 1, "the cluster must still be running"
    # and the take-profit test now has to earn that loss back first
    assert a.core.check_close(100.0, 770.0, 770.02) is False
    assert a.core.check_close(abs(expected) + 78.0, 770.0, 770.02) is True


def test_settlement_is_deferred_while_an_assignment_is_open():
    patch_settlement()
    cli = StubCli(is_open=False, timestamp="2026-09-05T00:00:03-04:00",
                  positions=[{"symbol": "SPY", "qty": "200", "side": "long"}])
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    a.handle_expiries("2026-09-05", {p['symbol']: p for p in cli.positions()})
    assert len(a.core.legs) == 1, "the leg must stay until the stock is flat"
    assert a.core.sunk_pot == 0.0, "nothing may be booked twice"


def test_missing_settlement_bar_fails_loud():
    patch_settlement(monkey_close=None)          # fetch returns []
    cli = StubCli(is_open=False, timestamp="2026-09-05T00:00:03-04:00")
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    try:
        a.handle_expiries("2026-09-05", {p['symbol']: p for p in cli.positions()})
    except agent_mod.AlpacaCliError as exc:
        assert "cannot settle" in str(exc), exc
    else:
        raise AssertionError("a missing settlement bar must not be guessed")
    assert len(a.core.legs) == 1, "the cluster must stay untouched"
    assert a.core.sunk_pot == 0.0


def test_assignment_gate_runs_before_the_settlement():
    """The order matters: an assigned leg must not be settled by intrinsic
    while the account still holds the stock."""
    patch_settlement()
    cli = StubCli(is_open=True, timestamp="2026-09-08T10:00:00-04:00")
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    seen = []
    a.assignment_gate = lambda pos: seen.append("assignment")
    orig = a.handle_expiries
    a.handle_expiries = lambda d, pos: (seen.append("expiries"), orig(d, pos))[1]
    a.stock_quote_called = False
    try:
        a.step(cli.clock(), {p['symbol']: p for p in cli.positions()})
    except Exception:
        pass          # the stub has no quotes; the order is what we check
    assert seen[:2] == ["assignment", "expiries"], seen


def test_old_state_file_without_the_pot_is_refused():
    from kangaroo_core import KangarooCore
    core = KangarooCore(rebuy_1st_pct=1.2, rebuy_pct=0.1, tp_pct=0.1,
                        initial_qty=1, max_invest_count=20)
    try:
        core.restore({"is_long": True, "cluster_id": 3, "legs": []})
    except KeyError as exc:
        assert "sunk_pot" in str(exc), exc
    else:
        raise AssertionError("a pre-pot state file must not load silently")


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
