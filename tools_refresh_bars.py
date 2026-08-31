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

"""Refill data/<sym>_1h.json for several symbols through ONE session filter.

fetch_alpaca_bars.py returns what Alpaca sends, which includes extended
hours: measured 2026-08-31 for AAPL over 2026-08-03..08-28 that is 320 bars
against 133 inside the exchange session, i.e. 187 bars (58 %) outside it.
Option bars do not exist for those hours, so a store built from the raw
fetch either aborts the simulator or - worse - shifts every entry to a
different bar than the store the GUI engine builds, which applies the
filter (run_engine._ensure_hour_bars:205-219).

This tool applies exactly that filter, so a store refilled here and one
built by the GUI are the same file. It is a WRITE-THROUGH refresh: the file
is replaced, never merged, so a stale extended-hours store cannot survive
inside a fresh one.

Usage:
    python tools_refresh_bars.py 2026-08-03 2026-08-29 AAPL AMD ...
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

from fetch_alpaca_bars import fetch, session_closes

HERE = os.path.dirname(os.path.abspath(__file__))
NY = ZoneInfo("America/New_York")


def session_bars(symbol: str, start: str, end: str,
                 closes: dict[str, str]) -> list[dict]:
    """Hourly bars inside the exchange session, same rule as the engine."""
    out = []
    for b in fetch(symbol, "1Hour", start, end):
        local = dt.datetime.fromisoformat(
            b["t"].replace("Z", "+00:00")).astimezone(NY)
        day = local.date().isoformat()
        if day not in closes:          # exchange holiday - no session
            continue
        # A bar stamped H covers H..H+1, so it belongs to the session only
        # while H is strictly before the day's close hour.
        if 9 <= local.hour < int(closes[day].split(":")[0]):
            out.append(b)
    out.sort(key=lambda b: b["t"])
    return out


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    start, end = sys.argv[1], sys.argv[2]
    symbols = [s.upper() for s in sys.argv[3:]]
    closes = session_closes(start, end)
    print(f"{len(closes)} exchange sessions in {start}..{end}")
    for sym in symbols:
        bars = session_bars(sym, start, end, closes)
        if not bars:
            raise RuntimeError(f"no RTH hourly bars for {sym} {start}..{end}")
        path = os.path.join(HERE, "data", f"{sym.lower()}_1h.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"symbol": sym, "timeframe": "1Hour", "bars": bars,
                       "next_page_token": ""}, fh)
        os.replace(tmp, path)
        days = len({b["t"][:10] for b in bars})
        print(f"{sym:6s} {len(bars):5d} RTH bars over {days:3d} days  "
              f"{bars[0]['t'][:13]} .. {bars[-1]['t'][:13]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
