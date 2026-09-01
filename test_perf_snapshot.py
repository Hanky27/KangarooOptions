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
import datetime as dt

from tools_perf_snapshot import (daily_spine, fee_timeline, fetch_curve, funding,
                                 realized_by_contract, split_activities)

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
           "last_equity": "99105.88", "equity": "90951.33"}
CLOCK = {"timestamp": "2026-09-01T12:25:00-04:00"}

ET = dt.timezone(dt.timedelta(hours=-4))


def at(stamp: str) -> int:
    """Epoch seconds for a market-time stamp.

    Computed rather than written out: fetch_curve buckets points into days
    with exactly this conversion, and a hand-typed epoch that lands on the
    wrong side of a boundary would make these tests pass or fail for a
    reason that has nothing to do with the code.
    """
    return int(dt.datetime.fromisoformat(stamp).replace(tzinfo=ET).timestamp())


# The daily series stamps a close at 20:00 ET; intraday bars sit inside
# the 09:30-16:00 window the per-day query asks for.
THU_CLOSE = at("2026-08-27T20:00:00")
FRI_CLOSE = at("2026-08-28T20:00:00")
MON_CLOSE = at("2026-08-31T20:00:00")
FRI_BARS = [(at("2026-08-28T09:30:00"), 100000.0),
            (at("2026-08-28T16:00:00"), 100000.0)]
MON_BARS = [(at("2026-08-31T09:30:00"), 200000.0),
            (at("2026-08-31T16:00:00"), 199130.16)]
TUE_BARS = [(at("2026-09-01T09:30:00"), 98593.88),
            (at("2026-09-01T12:20:00"), 95437.88)]


class SpineCli:
    """Answers the three shapes of query fetch_curve makes."""

    def __init__(self, daily, per_day, base_value="99105.88"):
        self.daily = daily
        self.per_day = per_day
        self.base_value = base_value
        self.asked = []

    def _run(self, args):
        self.asked.append(list(args))
        if "--period" in args and args[args.index("--period") + 1] == "1M":
            return self.daily
        if "--period" in args and args[args.index("--period") + 1] == "1D":
            bars = self.per_day.get("2026-09-01", [])
            return {"base_value": self.base_value,
                    "timestamp": [t for t, _ in bars],
                    "equity": [e for _, e in bars]}
        day = args[args.index("--start") + 1][:10]
        bars = self.per_day.get(day, [])
        return {"base_value": self.base_value,
                "timestamp": [t for t, _ in bars],
                "equity": [e for _, e in bars]}


def _daily(points):
    return {"timestamp": [t for t, _ in points],
            "equity": [e for _, e in points]}


SPINE = _daily([(THU_CLOSE, 0.0), (FRI_CLOSE, 100000.0),
                (MON_CLOSE, 99105.88)])


def test_the_spine_must_be_the_account_the_broker_reports():
    """The daily series was the only query that read this account
    correctly in every measurement. It is still not trusted on that
    record: its newest close has to equal last_equity, which is the same
    number arriving by a different route."""
    wrong = _daily([(FRI_CLOSE, 100000.0), (MON_CLOSE, 91000.0)])
    with pytest.raises(AlpacaCliError) as err:
        daily_spine(SpineCli(wrong, {}), ACCOUNT)
    assert "last_equity" in str(err.value)
    assert "91,000.00" in str(err.value)


def test_days_before_the_account_existed_are_dropped_from_the_spine():
    """The broker reports them as equity 0, and a zero in the curve is a
    100 % drawdown in the drawdown line."""
    spine = daily_spine(SpineCli(SPINE, {}), ACCOUNT)
    assert [t for t, _ in spine] == [FRI_CLOSE, MON_CLOSE]
    assert 0.0 not in [e for _, e in spine]


def test_the_funding_days_intraday_line_is_refused_and_named():
    """Measured 2026-09-01: the window over Monday - the day the account
    was funded - returned 200,000.00 for an open the account held 100,000
    at, and 199,130.16 for a close the broker reports as 99,105.88. That
    line was published for eighteen hours. It contributes its daily close
    instead, and the refusal is recorded rather than silently applied."""
    cli = SpineCli(SPINE, {"2026-08-28": FRI_BARS, "2026-08-31": MON_BARS})
    points, checks = fetch_curve(cli, ACCOUNT, CLOCK)
    assert "2026-08-31" not in checks["intraday_days"]
    assert "2026-08-28" in checks["intraday_days"]
    refused = [r for r in checks["rejected_days"] if r["day"] == "2026-08-31"]
    assert refused and refused[0]["intraday_close"] == 199130.16
    assert refused[0]["daily_close"] == 99105.88
    assert 199130.16 not in [e for _, e in points]
    assert (MON_CLOSE, 99105.88) in points


def test_a_day_that_lands_on_its_close_keeps_its_shape():
    cli = SpineCli(SPINE, {"2026-08-28": FRI_BARS})
    points, checks = fetch_curve(cli, ACCOUNT, CLOCK)
    assert checks["intraday_days"] == ["2026-08-28"]
    for stamp, value in FRI_BARS:
        assert (stamp, value) in points


def test_todays_bars_are_refused_when_they_disagree_with_the_account():
    """Today has no close to check against. Measured at 12:24 ET: the
    newest 5Min bar read 95,595.88 while the account reported 90,321.33
    and cash+marks agreed with the account to within 134.00 - and the
    12:15 bar was revised down by 961 between two reads. Drawing those
    would put the line 5,000 above the headline and end it in a cliff."""
    cli = SpineCli(SPINE, {"2026-08-28": FRI_BARS, "2026-09-01": TUE_BARS})
    points, checks = fetch_curve(cli, ACCOUNT, CLOCK)
    assert "2026-09-01" not in checks["intraday_days"]
    refused = [r for r in checks["rejected_days"] if r["day"] == "2026-09-01"]
    assert refused, checks["rejected_days"]
    assert refused[0]["gap"] == round(95437.88 - 90951.33, 2)
    assert refused[0]["allowed"] == round(90951.33 * 0.01, 2)
    assert 95437.88 not in [e for _, e in points]


def test_todays_bars_are_kept_when_they_are_merely_lagging():
    """The check is about lag, not about truth: the newest bar is minutes
    old and the account is now, so a normal gap has to pass."""
    close_enough = [(at("2026-09-01T09:30:00"), 98593.88),
                    (at("2026-09-01T12:20:00"), 91100.0)]
    cli = SpineCli(SPINE,
                   {"2026-08-28": FRI_BARS, "2026-09-01": close_enough})
    _points, checks = fetch_curve(cli, ACCOUNT, CLOCK)
    assert "2026-09-01" in checks["intraday_days"]
    assert not [r for r in checks["rejected_days"]
                if r["day"] == "2026-09-01"]


def test_todays_bars_are_refused_when_the_baseline_is_not_yesterdays_close():
    cli = SpineCli(SPINE, {"2026-08-28": FRI_BARS,
                           "2026-09-01": [(at("2026-09-01T12:20:00"),
                                           91100.0)]},
                   base_value="100000")
    _points, checks = fetch_curve(cli, ACCOUNT, CLOCK)
    assert "2026-09-01" not in checks["intraday_days"]


# --- starting capital ---------------------------------------------------

def test_starting_capital_is_read_from_the_accounts_own_funding():
    """Not from base_value: that is whichever baseline the history
    endpoint picked, and it was measured naming 2026-08-27, a date on
    which this account did not exist."""
    first_fill = int(dt.datetime.fromisoformat(
        "2026-08-31T13:40:20+00:00").timestamp())
    transfers = [{"net_amount": "100000",
                  "created_at": "2026-08-31T06:13:40.396198Z"}]
    assert funding(transfers, first_fill) == 100000.0


def test_money_moved_after_trading_began_is_not_starting_capital():
    """It is not performance either - the identity carries it as its own
    term so it can never be read as profit."""
    first_fill = int(dt.datetime.fromisoformat(
        "2026-08-31T13:40:20+00:00").timestamp())
    transfers = [{"net_amount": "100000",
                  "created_at": "2026-08-31T06:13:40.396198Z"},
                 {"net_amount": "25000",
                  "created_at": "2026-09-01T15:00:00.000000Z"}]
    assert funding(transfers, first_fill) == 100000.0
