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

"""Self-tests of the Kangaroo Options core. Run: python test_kangaroo_core.py"""

from kangaroo_core import KangarooCore


def make_core(**overrides) -> KangarooCore:
    params = dict(rebuy_1st_pct=1.2, rebuy_pct=0.10, tp_pct=0.10,
                  initial_qty=1, max_invest_count=20, start_long=True)
    params.update(overrides)
    return KangarooCore(**params)


def test_initial_open():
    core = make_core()
    assert core.check_rebuy(100.0, 100.02) == 1, "empty cluster must open initially"


def test_first_rebuy_uses_rebuy_1st_pct():
    core = make_core()
    core.add_leg("C1", 1, entry_underlying=100.0, entry_premium=1.0)
    # 1st rebuy threshold: ask < 100 * (1 - 1.2/100) = 98.8
    assert core.check_rebuy(98.79, 98.81) == 0, "98.81 ask is not below 98.8"
    assert core.check_rebuy(98.77, 98.79) == 1, "98.79 ask is below 98.8"


def test_later_rebuys_use_rebuy_pct_and_last_leg_reference():
    core = make_core()
    core.add_leg("C1", 1, entry_underlying=100.0, entry_premium=1.0)
    core.add_leg("C2", 1, entry_underlying=98.7, entry_premium=1.2)
    # invest_count == 2 -> rebuy_pct 0.10 from LAST leg 98.7 -> 98.6013
    assert core.check_rebuy(98.61, 98.62) == 0
    assert core.check_rebuy(98.58, 98.60) == 1


def test_put_cluster_triggers_on_rising_bid():
    core = make_core(start_long=False)
    core.add_leg("P1", 1, entry_underlying=100.0, entry_premium=1.0)
    # short side references the BID: trigger above 100 * 1.012 = 101.2
    assert core.check_rebuy(101.19, 101.21) == 0, "bid 101.19 is not above 101.2"
    assert core.check_rebuy(101.21, 101.23) == 1


def test_qty_growth_rounding():
    core = make_core()
    got = []
    for n in range(8):
        while core.invest_count < n:
            core.add_leg(f"X{core.invest_count}", 1, 100.0, 1.0)
        got.append(core.next_leg_qty())
    # 1.1^n for n=0..7 -> 1.0 1.1 1.21 1.331 1.4641 1.61 1.77 1.95
    assert got == [1, 1, 1, 1, 1, 2, 2, 2], got


def test_max_invest_count_cap():
    core = make_core(max_invest_count=2)
    core.add_leg("A", 1, 100.0, 1.0)
    core.add_leg("B", 1, 90.0, 1.0)
    assert core.check_rebuy(1.0, 1.1) == 0, "cap reached - no more legs"


def test_cluster_profit_and_take_profit():
    core = make_core()
    core.add_leg("C1", 1, entry_underlying=100.0, entry_premium=1.00)
    core.add_leg("C2", 2, entry_underlying=98.8, entry_premium=0.80)
    bids = {"C1": 1.30, "C2": 1.00}
    profit = core.cluster_profit_usd(bids)
    # (1.30-1.00)*100*1 + (1.00-0.80)*100*2 = 30 + 40 = 70 USD
    assert abs(profit - 70.0) < 1e-9, profit
    # threshold at spot_bid 100: 2 legs * 1 * 100 * (0.10% of 100) = 20 USD
    assert core.check_close(70.0, 100.0, 100.02) is True
    assert core.check_close(19.99, 100.0, 100.02) is False
    # exactly at threshold is NOT a close (original: strictly greater)
    assert core.check_close(20.0, 100.0, 100.02) is False


def test_mode1_toggle_on_close():
    core = make_core()
    core.add_leg("C1", 1, 100.0, 1.0)
    assert core.is_long is True and core.cluster_id == 1
    core.on_cluster_closed()
    assert core.is_long is False, "Mode1 must toggle to the PUT cluster"
    assert core.cluster_id == 2
    assert core.invest_count == 0
    core.on_cluster_closed()
    assert core.is_long is True, "and back to CALL"


def test_no_toggle_close_keeps_direction():
    core = make_core()
    core.add_leg("P1", 1, 100.0, 1.0)
    core.on_cluster_closed(toggle=False)
    assert core.is_long is True, "toggle=False must keep the direction"
    assert core.cluster_id == 2 and core.invest_count == 0


def test_persistence_roundtrip():
    core = make_core()
    core.add_leg("SPY260904C00780000", 2, 770.25, 1.07)
    core.on_cluster_closed()
    core.add_leg("SPY260904P00760000", 1, 771.18, 1.55)
    snapshot = core.to_dict()
    other = make_core()
    other.restore(snapshot)
    assert other.to_dict() == snapshot


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
