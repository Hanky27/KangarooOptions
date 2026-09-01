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

"""Regression tests for the two defects the published sheet carried.

Both were found live on 2026-09-01 and both are reproduced here from the
account data behind them, not from invented numbers.
"""

import pytest

from alpaca_cli import AlpacaCliError
from tools_perf_snapshot import (fee_timeline, fetch_curve, realized_by_contract,
                                 split_activities)

# The account's own bookings, in the shape and the order the CLI returned
# them: OCC clearing at -0.03 and -0.05 through the session, then the four
# regulatory charges that settled after Monday's close. The funding journal
# is in here too, because it is what the classifier has to keep out of the
# fee total. Trimmed to a readable size; the ratios are the real ones.
BOOKINGS = [
    {"activity_type": "JNLC", "net_amount": "100000",
     "created_at": "2026-08-31T06:13:40.396198Z",
     "id": "20260831000000000::130747e6"},
    {"activity_type": "FEE", "activity_sub_type": "OCC", "net_amount": "-0.03",
     "created_at": "2026-08-31T13:40:20.728296Z",
     "id": "20260831000000000::0e433c66"},
    {"activity_type": "FEE", "activity_sub_type": "OCC", "net_amount": "-0.05",
     "created_at": "2026-08-31T16:35:45.867866Z",
     "id": "20260831000000000::035d4704"},
    {"activity_type": "FEE", "activity_sub_type": "ORF", "net_amount": "-4.65",
     "created_at": "2026-08-31T20:01:08.403215Z",
     "id": "20260831000000000::00d332fc"},
    {"activity_type": "FEE", "activity_sub_type": "REG", "net_amount": "-0.79",
     "created_at": "2026-08-31T20:02:43.225982Z",
     "id": "20260831000000000::0ac6a63a"},
    {"activity_type": "FEE", "activity_sub_type": "TAF", "net_amount": "-0.44",
     "created_at": "2026-08-31T20:02:43.300000Z",
     "id": "20260831000000000::0ac6a63b"},
    {"activity_type": "FEE", "activity_sub_type": "CAT", "net_amount": "-0.10",
     "created_at": "2026-09-01T00:06:54.908841Z",
     "id": "20260901000000000::0ac6a63c"},
]


def test_fees_are_the_brokers_bookings_not_a_rate():
    """The report used to model the fee as 0.025 USD per contract, which
    was right while the OCC clearing fee was the only booking. The
    regulatory charges settle after the close at their own rates and no
    per-contract constant reaches them: measured on the live account,
    15.12 booked against 7.75 modelled, and the 7.37 difference aborted 20
    consecutive publishes."""
    fees, transfers = split_activities(BOOKINGS)
    assert len(fees) == 6
    assert len(transfers) == 1
    total = fee_timeline(fees)[-1]["fees"]
    assert round(total, 2) == 6.06, total
    # A per-contract rate cannot produce this: every one of the six
    # bookings carries a different amount.
    assert len({f["net_amount"] for f in fees}) == 6


def test_the_fee_line_runs_in_booking_order():
    """`--direction asc` sorts by the activity's date and pages on an id
    that carries only a day stamp, so a day's rows come back in uuid
    order - measured over 299 rows, first created_at 18:29:19Z and last
    15:19:08Z. A cumulative line built on that order would zig-zag."""
    shuffled = [BOOKINGS[4], BOOKINGS[1], BOOKINGS[6], BOOKINGS[3],
                BOOKINGS[2], BOOKINGS[5]]
    line = fee_timeline(shuffled)
    assert [p["t"] for p in line] == sorted(p["t"] for p in line)
    assert all(b["fees"] >= a["fees"]
               for a, b in zip(line, line[1:])), line


def test_an_unclassifiable_booking_stops_the_report():
    """A dividend, an assignment or a correction moves the account by an
    amount that is not trading performance. Folding it into either side
    would misstate the result, so the run ends and the previous snapshot
    stays up."""
    with pytest.raises(AlpacaCliError) as err:
        split_activities(BOOKINGS + [{"activity_type": "DIV",
                                      "net_amount": "12.00",
                                      "created_at": "2026-09-01T10:00:00Z",
                                      "id": "x"}])
    assert "DIV" in str(err.value)


def test_realized_carries_no_fees_any_more():
    """Fees are the broker's bookings, not a consequence of the fills.
    The version that accrued them here is what put the hole in the
    reconciliation."""
    fills = [
        {"symbol": "SPY260904P00770000", "qty": "1", "side": "sell_short",
         "price": "2.00", "transaction_time": "2026-08-31T13:40:20+00:00"},
        {"symbol": "SPY260904P00770000", "qty": "1", "side": "buy",
         "price": "1.00", "transaction_time": "2026-08-31T14:40:20+00:00"},
    ]
    realized, _per, closed, events = realized_by_contract(fills)
    assert round(realized, 2) == 100.0
    assert closed == 1
    assert all("fees" not in e for e in events), events


# --- the curve ----------------------------------------------------------

ACCOUNT = {"created_at": "2026-08-31T06:12:32.878183Z",
           "last_equity": "99105.88"}

# Thursday and Friday, before the account existed: the broker reports the
# equity of an account that was not there as 0. Monday is the live day.
BEFORE = [1787836200, 1787922600]
MONDAY = [1788183000, 1788186600, 1788204600, 1788206400]


def _series(base, timestamps, equities):
    return {"base_value": base,
            "base_value_asof": "2026-08-27",
            "timestamp": list(timestamps),
            "equity": list(equities),
            "profit_loss": [e - float(base) for e in equities]}


class CurveCli:
    """Answers the two portfolio queries fetch_curve makes."""

    def __init__(self, five_min, one_hour):
        self.five_min = five_min
        self.one_hour = one_hour

    def _run(self, args):
        return self.five_min if "5Min" in args else self.one_hour


def test_a_curve_that_is_not_this_account_is_refused():
    """Measured 2026-09-01: `--period 1D --timeframe 5Min` returned
    200,000 for the open of a day the account started at 100,000, and
    199,130.16 for its close against the broker's own last_equity of
    99,105.88. The published page drew a flat line at ~199,000 and a max
    drawdown of -102,137.75 that never happened. Nothing in the output
    said so, which is why the series is checked against a second one."""
    inflated = _series("100000", MONDAY,
                       [200000.0, 199624.94, 199097.40, 199130.16])
    honest = _series("100000", MONDAY,
                     [100000.0, 99624.94, 99097.40, 99130.16])
    with pytest.raises(AlpacaCliError) as err:
        fetch_curve(CurveCli(inflated, honest), ACCOUNT)
    assert "100,000.00" in str(err.value), str(err.value)


def test_the_two_resolutions_may_differ_by_the_marks_they_sample():
    """5Min and 1H bars are sampled at different instants, so they never
    agree to the cent. Measured spread on the live account: at most 111
    USD on 99,000. The check has to pass that and still catch 100,000."""
    fine = _series("100000", MONDAY,
                   [100000.0, 99624.94, 99097.40, 99130.16])
    coarse = _series("100000", MONDAY,
                     [100000.0, 99613.94, 99097.40, 99019.16])
    history, checks = fetch_curve(CurveCli(fine, coarse), ACCOUNT)
    assert checks["curve_vs_control"] == 111.0
    assert checks["curve_tolerance"] == 1000.0


def test_points_from_before_the_account_existed_are_dropped():
    """The broker reports them as equity 0. Left in, a zero is a 100 %
    drawdown in the drawdown line - the same defect in a second guise."""
    stamps = BEFORE + MONDAY
    equities = [0.0, 0.0, 100000.0, 99624.94, 99097.40, 99130.16]
    series = _series("100000", stamps, equities)
    history, _checks = fetch_curve(CurveCli(series, series), ACCOUNT)
    assert history["timestamp"] == MONDAY
    assert 0.0 not in history["equity"]
    assert len(history["equity"]) == len(history["profit_loss"]) == 4


def test_the_previous_close_is_checked_against_the_brokers_own_figure():
    """A second, independent anchor: whatever the curve says yesterday
    closed at has to be what the account says it closed at."""
    tuesday = [1788269400, 1788271200]
    stamps = MONDAY + tuesday
    good = _series("100000", stamps,
                   [100000.0, 99624.94, 99097.40, 99105.88, 98900.0, 97854.88])
    history, checks = fetch_curve(CurveCli(good, good), ACCOUNT)
    assert checks["curve_vs_last_equity"] == 0.0
    assert history["timestamp"] == stamps

    bad = _series("100000", stamps,
                  [100000.0, 99624.94, 99097.40, 92000.0, 98900.0, 97854.88])
    with pytest.raises(AlpacaCliError) as err:
        fetch_curve(CurveCli(bad, bad), ACCOUNT)
    assert "last_equity" in str(err.value)


def test_the_first_day_has_no_previous_close_to_check():
    """None, not a false pass: on the account's first day the second
    anchor does not exist yet and the report says so."""
    only_monday = _series("100000", MONDAY,
                          [100000.0, 99624.94, 99097.40, 99130.16])
    _history, checks = fetch_curve(CurveCli(only_monday, only_monday), ACCOUNT)
    assert checks["curve_vs_last_equity"] is None
