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
import alpaca_cli
import risk_gate
from agent import (Instrument, SingleInstance,
                   load_instrument_configs)
from kangaroo_core import settlement_pnl

SETTLE_CLOSE = 767.00      # underlying close of the expiry day


class StubCli:
    """Minimal stand-in for AlpacaCli: records calls, returns fixtures."""

    def __init__(self, *, is_open: bool, timestamp: str, positions=None,
                 orders=None):
        self._is_open = is_open
        self._timestamp = timestamp
        self._positions = positions or []
        self.orders = orders or []
        self.calls: list[str] = []

    def clock(self):
        self.calls.append("clock")
        return {"timestamp": self._timestamp, "is_open": self._is_open,
                "next_open": "-", "next_close": "-"}

    def positions(self):
        self.calls.append("positions")
        return list(self._positions)

    def orders_since(self, iso_ts, limit=500):
        self.calls.append("orders_since")
        return list(self.orders)

    def account(self):
        self.calls.append("account")
        return {"account_number": "STUB", "options_approved_level": 3,
                "options_trading_level": 3, "equity": 100000}


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


class _Core:
    cluster_id, invest_count = 1, 0


class StubInstrument:
    """The instrument protocol the loader actually uses, nothing more."""

    def __init__(self, name, fail_step=False, fail_start=False):
        self.name, self.underlying = name, name.split("_")[0]
        self.core, self.state_file = _Core(), f"{name}.json"
        self.halted, self.failures, self.steps = False, 0, 0
        self._fail_step, self._fail_start = fail_step, fail_start
        self.said = []

    def say(self, msg, ts=None):
        self.said.append(msg)

    def load_state(self):
        if self._fail_start:
            raise RuntimeError("state file does not match the account")

    def handle_expiries(self, day, positions):
        pass

    def recover_unbooked_close(self, positions, orders):
        return False

    def reconcile(self, positions):
        pass

    def leg_symbols(self):
        return []

    def step(self, *a, **kw):
        # The loader hands every instrument the gate and the portfolio
        # view as keywords; a stub that refuses them would fail for a
        # reason that has nothing to do with what these tests measure.
        self.gate_seen = kw.get("gate")
        if self._fail_step:
            raise RuntimeError("this instrument is broken")
        self.steps += 1


def _loader_with(instruments, polls):
    a = agent_mod.KangarooAgent.__new__(agent_mod.KangarooAgent)
    a.dry_run, a.poll_seconds = True, 0
    a.instruments, a._reported_halted = instruments, []
    # A loader always has a gate; disabled is the default and costs nothing,
    # so these tests exercise the same code path as a run without a key.
    a.gate = risk_gate.RiskGate({}, say=lambda *_a, **_k: None)
    a.cli = StubCli(is_open=False, timestamp="2026-08-31T10:00:00-04:00")
    for _ in range(polls):
        a.run(once=True)
    return a


def test_a_broken_instrument_does_not_stop_a_healthy_one():
    bad = StubInstrument("BAD_long", fail_step=True)
    good = StubInstrument("GOOD_long")
    _loader_with([bad, good], polls=3)
    assert good.steps == 3, f"healthy instrument stepped {good.steps}/3"
    assert bad.failures == 3, bad.failures
    assert not bad.halted, "3 failures is below the halt threshold"


def test_a_persistently_broken_instrument_is_halted_and_named():
    bad = StubInstrument("BAD_long", fail_step=True)
    good = StubInstrument("GOOD_long")
    _loader_with([bad, good], polls=agent_mod.HALT_AFTER + 2)
    assert bad.halted, "it must stop trying after HALT_AFTER polls"
    assert bad.failures == agent_mod.HALT_AFTER, bad.failures
    assert good.steps == agent_mod.HALT_AFTER + 2, good.steps
    assert any("HALTED" in m for m in bad.said), bad.said


def test_a_clean_poll_resets_the_failure_count():
    flaky = StubInstrument("FLAKY_long", fail_step=True)
    good = StubInstrument("GOOD_long")
    a = _loader_with([flaky, good], polls=2)
    assert flaky.failures == 2, flaky.failures
    flaky._fail_step = False
    a.run(once=True)
    assert flaky.failures == 0, "a clean poll must clear the count"
    assert not flaky.halted


def test_a_startup_failure_halts_only_that_instrument():
    bad = StubInstrument("BAD_long", fail_start=True)
    good = StubInstrument("GOOD_long")
    _loader_with([bad, good], polls=2)
    assert bad.halted, "a bad state file must halt its own instrument"
    assert bad.steps == 0, "a halted instrument must not trade"
    assert good.steps == 2, f"healthy instrument stepped {good.steps}/2"
    assert any("HALTED AT STARTUP" in m for m in bad.said), bad.said


def test_one_agent_at_a_time():
    """The watchdog restarts the agent whenever it looks dead, so this lock
    is the only thing between a false positive and two processes writing
    the same state files."""
    lp = os.path.join(tempfile.mkdtemp(prefix="kang_lock_"), "agent.lock")
    first = SingleInstance(lp)
    first.acquire()
    with open(lp) as fh:
        assert fh.read(64).strip() == str(os.getpid())
    try:
        SingleInstance(lp).acquire()
    except agent_mod.AlpacaCliError as exc:
        assert str(os.getpid()) in str(exc), exc
    else:
        raise AssertionError("a second agent must not get the lock")
    first.release()
    again = SingleInstance(lp)
    again.acquire()          # released -> the next process may have it
    again.release()


HOLDER_SRC = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from agent import SingleInstance\n"
    "s = SingleInstance(sys.argv[2]); s.acquire()\n"
    "print('held', flush=True)\n"
    "time.sleep(120)\n"
)


def test_a_killed_agent_leaves_no_lock_behind():
    """A pid file would survive a kill and lie. An OS lock does not: the
    kernel drops it however the process dies, so a crashed agent can be
    restarted while a live one still cannot be doubled."""
    import subprocess
    repo = os.path.dirname(os.path.abspath(agent_mod.__file__))
    lp = os.path.join(tempfile.mkdtemp(prefix="kang_kill_"), "agent.lock")
    proc = subprocess.Popen([sys.executable, "-c", HOLDER_SRC, repo, lp],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held", "holder did not start"
        try:
            SingleInstance(lp).acquire()
        except agent_mod.AlpacaCliError:
            pass
        else:
            raise AssertionError("the live holder must block a second agent")
    finally:
        proc.kill()
        proc.wait()
    survivor = SingleInstance(lp)
    survivor.acquire()       # must NOT raise - the kill freed the lock
    survivor.release()


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


class MixedGridCli:
    """The TLT chain of 2026-09-02, measured against the broker: whole
    dollars below 80, half dollars above. This is the shape that halted
    TLT_long - the short strike lands on 82.5 and every configured width
    points at a half dollar under 80 that does not exist."""

    def __init__(self):
        self.strikes = ([float(k) for k in range(70, 80)]
                        + [80.0, 80.5, 81.0, 81.5, 82.0, 82.5, 83.0, 83.5])
        self.asked = None

    @staticmethod
    def occ(underlying, right, strike):
        return (f"{underlying}260902{right[0].upper()}"
                f"{int(strike * 1000):08d}")

    def option_contracts(self, underlying, gte, lte, right):
        self.asked = right
        self.underlying = underlying
        return [{"symbol": self.occ(underlying, right, k),
                 "strike_price": k, "expiration_date": "2026-09-02",
                 "tradable": True} for k in self.strikes]

    def option_quotes(self, occs):
        # Priced BY STRIKE, so a put credit spread (sell the higher strike,
        # buy the lower) always carries a positive credit. A flat price
        # would make every candidate fail the credit check and the test
        # would then be measuring the wrong thing.
        out = {}
        for occ in occs:
            strike = int(occ[-8:]) / 1000.0
            bp = strike * 0.10
            out[occ] = {"bp": round(bp, 2), "ap": round(bp + 0.02, 2),
                        "t": "2026-08-31T13:44"}
        return out


def test_a_strike_held_long_is_not_sold_as_the_short_leg():
    """The rebuy steps one wing-width further out, which lands exactly on
    the previous leg's wing. Selling a contract the account is long does
    not open a short leg - it closes the wing, and the broker says so:
    422 "position intent mismatch, inferred: sell_to_close". Measured on
    GOOGL_long, leg 1 = 345/340, rebuy = 340/335."""
    cli = MixedGridCli()
    cli.strikes = [float(k) for k in range(330, 351)]
    cfg = {"underlying": "GOOGL", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 1, "dte_max": 4,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30, "start_long": True}
    inst = Instrument(cfg, cli, True, tempfile.mkdtemp(prefix="kang_net_"))
    # Leg 1 already on the books: short 345, long 340 as the wing.
    def occ(k):
        return MixedGridCli.occ("GOOGL", "put", k)
    positions = {occ(345.0): {"symbol": occ(345.0), "qty": "-1"},
                 occ(340.0): {"symbol": occ(340.0), "qty": "1"}}
    spread = inst.select_spread(340.0, date(2026, 8, 31), positions)
    assert spread["strike"] != 340.0, (
        "340 is held LONG - selling it closes the wing", spread)
    assert spread["wing_strike"] != 345.0, (
        "345 is held SHORT - buying it closes that leg", spread)
    assert spread["wing_strike"] < spread["strike"], spread


def test_without_positions_the_nearest_strike_is_still_taken():
    """No account snapshot -> nothing to net against -> unchanged
    behaviour. The netting guard must not move strikes on its own."""
    cli = MixedGridCli()
    cli.strikes = [float(k) for k in range(330, 351)]
    cfg = {"underlying": "GOOGL", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 1, "dte_max": 4,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30, "start_long": True}
    inst = Instrument(cfg, cli, True, tempfile.mkdtemp(prefix="kang_net2_"))
    spread = inst.select_spread(340.0, date(2026, 8, 31))
    assert spread["strike"] == 340.0, spread
    assert spread["wing_strike"] == 335.0, spread


def test_wing_snaps_to_the_nearest_existing_strike():
    """Exact-match wing selection halted a live instrument for a whole day.
    The simulator takes the nearest EXISTING strike; the agent must too, or
    the bot trades something other than what was measured."""
    cli = MixedGridCli()
    cfg = {"underlying": "TLT", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 1, "dte_max": 4,
           "spread_widths": [5, 6, 4, 7, 3, 8, 10], "poll_fill_seconds": 2,
           "fill_requote_samples": 30, "start_long": True}
    inst = Instrument(cfg, cli, True, tempfile.mkdtemp(prefix="kang_wing_"))
    spread = inst.select_spread(82.4, date(2026, 8, 31))
    assert spread["strike"] == 82.5, spread
    # 82.5 - 5 = 77.5 does not exist; 77.0 and 78.0 do. Nearest wins.
    assert spread["wing_strike"] in (77.0, 78.0), spread
    assert spread["wing_strike"] < spread["strike"], spread
    assert spread["credit_limit"] > 0, spread


def test_a_side_with_no_strike_at_all_still_fails_loud():
    """Snapping to the nearest must not paper over an empty side."""
    cli = MixedGridCli()
    cli.strikes = [82.5, 83.0, 83.5]          # nothing below the short leg
    cfg = {"underlying": "TLT", "rebuy_1st_pct": 1.2, "rebuy_pct": 0.10,
           "tp_pct": 0.10, "initial_qty": 1, "max_invest_count": 20,
           "max_adverse_pct": 0.0, "dte_min": 1, "dte_max": 4,
           "spread_widths": [5], "poll_fill_seconds": 2,
           "fill_requote_samples": 30, "start_long": True}
    inst = Instrument(cfg, cli, True, tempfile.mkdtemp(prefix="kang_noside_"))
    try:
        inst.select_spread(82.4, date(2026, 8, 31))
    except agent_mod.AlpacaCliError as exc:
        # The wording covers both reasons a side can be unusable:
        # nothing there at all, or everything there held short.
        assert "no usable strike" in str(exc), exc
    else:
        raise AssertionError("an empty wing side must fail loud")


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


class BatchCli:
    """Records every symbol list the wrapper actually sends."""

    def __init__(self):
        self.batches = []

    def _run(self, args):
        symbols = args[args.index("--symbols") + 1].split(",")
        self.batches.append(symbols)
        return {"quotes": {s: {"bp": 1.0, "ap": 1.1} for s in symbols}}


def test_quote_requests_are_split_at_the_endpoints_symbol_limit():
    """Measured 2026-09-01 at 18:03 CEST: the grid crossed 100 open legs
    and every poll from then on asked for 102 symbols in one request. The
    endpoint answered {"error": "symbol limit is 100", "status": 400}
    with NO quotes at all, so market_data() raised before the first
    instrument was stepped. 25 grids with open positions went unmanaged
    for 93 minutes while the market was open, restarted by the watchdog
    every two minutes and killed the same way each time."""
    occs = [f"SPY2609{i:02d}P00700000" for i in range(102)]
    cli = alpaca_cli.AlpacaCli.__new__(alpaca_cli.AlpacaCli)
    recorder = BatchCli()
    cli._run = recorder._run

    quotes = alpaca_cli.AlpacaCli.option_quotes(cli, occs)
    assert len(quotes) == 102, len(quotes)
    assert [len(b) for b in recorder.batches] == [100, 2]
    assert sorted(sum(recorder.batches, [])) == sorted(occs)

    recorder.batches.clear()
    stock = alpaca_cli.AlpacaCli.stock_quotes(cli, ["SPY", "QQQ"])
    assert len(stock) == 2
    assert [len(b) for b in recorder.batches] == [2]


def test_a_list_at_exactly_the_limit_is_one_request():
    occs = [f"SPY2609{i:02d}P00700000" for i in range(100)]
    cli = alpaca_cli.AlpacaCli.__new__(alpaca_cli.AlpacaCli)
    recorder = BatchCli()
    cli._run = recorder._run
    alpaca_cli.AlpacaCli.option_quotes(cli, occs)
    assert [len(b) for b in recorder.batches] == [100]


def _closed_order(name, cluster, coid_kind="x"):
    return {"status": "filled",
            "client_order_id": f"kang_{name}_c{cluster}_l0_{coid_kind}_178"}


def test_a_close_the_broker_filled_is_recovered_on_startup():
    """Three times on 2026-08-31 a close order was FILLED while the agent
    was being stopped inside its 60 s fill window, so close_cluster never
    booked it. The agent came back believing it held a cluster the account
    no longer had. The broker is the authority: a filled _x_ order for this
    cluster plus no remaining position means the cluster ended."""
    cli = StubCli(is_open=True, timestamp="2026-08-31T10:00:00-04:00")
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    before = a.core.cluster_id
    ok = a.recover_unbooked_close({}, [_closed_order(a.name, before)])
    assert ok is True
    assert a.core.legs == [], a.core.legs
    assert a.core.cluster_id == before + 1, a.core.cluster_id
    assert a.leg_extras == {}


def test_recovery_needs_BOTH_the_order_and_the_missing_position():
    """Either signal alone is not enough. A filled close while a leg is
    still held can be a partial close; a missing position without a close
    order can be broker lag or a bad state file - discarding a real cluster
    on that basis would be far worse than halting."""
    cli = StubCli(is_open=True, timestamp="2026-08-31T10:00:00-04:00")

    # order present, but a leg is still in the account -> no recovery
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    held = {"SPY260904P00770000": {"symbol": "SPY260904P00770000",
                                   "qty": "-2"}}
    assert a.recover_unbooked_close(held,
                                    [_closed_order(a.name, 1)]) is False
    assert len(a.core.legs) == 1

    # position gone, but no close order -> no recovery either
    b = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    assert b.recover_unbooked_close({}, []) is False
    assert len(b.core.legs) == 1

    # an OPEN order of the same cluster is not a close
    c = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    assert c.recover_unbooked_close(
        {}, [_closed_order(c.name, 1, coid_kind="o")]) is False
    assert len(c.core.legs) == 1


def test_recovery_ignores_another_instruments_close():
    """The client_order_id carries instrument AND cluster; a close from a
    different grid must not end this one."""
    cli = StubCli(is_open=True, timestamp="2026-08-31T10:00:00-04:00")
    a = make_agent(cli, legs=[("SPY260904P00770000", 2, 1.20,
                               770.0, 765.0, "2026-09-04")])
    foreign = [_closed_order("QQQ_short", 1),
               _closed_order(a.name, a.core.cluster_id + 5)]
    assert a.recover_unbooked_close({}, foreign) is False
    assert len(a.core.legs) == 1


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
    try:
        # Empty quote maps: the instrument gets that far and then fails
        # loud on the missing quote. The ORDER before it is what is tested,
        # and only that one error is tolerated - a TypeError from a changed
        # signature must not pass for a missing quote.
        a.step(cli.clock(), {p['symbol']: p for p in cli.positions()}, {}, {})
    except agent_mod.AlpacaCliError:
        pass
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
