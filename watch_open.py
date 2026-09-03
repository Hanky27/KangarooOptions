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

"""Watch the opening bell, because today it runs code that never has.

The account holds four stock positions an assignment left behind - AMZN
-100, GLD +100, SLV +800, TLT +100, about 122,000 USD of direction nobody
chose. `assignment_gate` flattens them on the first poll with the market
open, and it has NEVER fired: measured 2026-09-03 across every log this
account has, zero assign_flat orders, because no assignment had yet met
an open market.

So the first minutes of this session exercise, live and for real money:
  - assignment_gate itself,
  - the fix that makes it remove the symbol from the shared position
    snapshot, without which AMZN_long and AMZN_short both buy 100 AMZN,
  - the settlement that handle_expiries deferred while the stock was held,
  - and reconcile's new exemption for a leg the broker settled.

This waits on EVENTS, not on a clock: it returns the moment the four
positions are flat, or the moment the account tells it something went
wrong. `--max-polls` is a backstop, not a schedule, and running into it is
reported as its own outcome rather than as success.

Usage:
    python watch_open.py --config config.yaml
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import yaml

from alpaca_cli import AlpacaCli, AlpacaCliError

HERE = os.path.dirname(os.path.abspath(__file__))


def stock_positions(cli: AlpacaCli) -> list[dict]:
    return [p for p in cli.positions() if p.get("asset_class") != "us_option"]


def say(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--poll-seconds", type=float, default=20.0)
    ap.add_argument("--max-polls", type=int, default=900,
                    help="backstop only; hitting it is reported as such")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cli = AlpacaCli(cfg["cli_path"], cfg.get("env_file"))

    start = stock_positions(cli)
    say(f"watching {len(start)} assigned stock position(s):")
    for p in start:
        say(f"   {p['symbol']:6} {p['qty']:>7}  at {p['avg_entry_price']:>10}"
            f"  mv {float(p['market_value']):>12,.2f}")
    if not start:
        say("nothing assigned is held - nothing to watch")
        return 0
    watched = {p["symbol"] for p in start}
    was_open = None

    for n in range(1, args.max_polls + 1):
        try:
            clock = cli.clock()
            now = clock["timestamp"][:19]
            if clock["is_open"] != was_open:
                say(f"[{now}] market {'OPEN' if clock['is_open'] else 'closed'}")
                was_open = clock["is_open"]
            if not clock["is_open"]:
                time.sleep(args.poll_seconds)
                continue

            held = {p["symbol"]: p for p in stock_positions(cli)}
            gone = watched - set(held)
            if gone:
                say(f"[{now}] flattened: {', '.join(sorted(gone))}")
                watched -= gone
            if not watched:
                acct = cli.account()
                eq, mm = float(acct["equity"]), float(acct["maintenance_margin"])
                say(f"[{now}] ALL FLAT. equity {eq:,.2f}  maintenance "
                    f"{mm:,.2f}  headroom {eq - mm:+,.2f}")
                # What the account says was actually sent - the gate's own
                # orders, by the client_order_id it stamps on them.
                orders = cli.orders_since(now[:10])
                flats = [o for o in orders
                         if "assign_flat" in str(o.get("client_order_id"))]
                say(f"assign_flat orders: {len(flats)}")
                for o in flats:
                    say(f"   {str(o.get('filled_at'))[:19]}  {o.get('symbol'):6} "
                        f"{o.get('side'):4} {o.get('filled_qty'):>6} @ "
                        f"{o.get('filled_avg_price')}  {o.get('status')}")
                # The failure this is really here for: one flatten per
                # underlying. Two means both directions acted on the same
                # stale snapshot row and the account is now long or short a
                # stock nobody chose.
                per = {}
                for o in flats:
                    per[o.get("symbol")] = per.get(o.get("symbol"), 0) + 1
                doubled = {k: v for k, v in per.items() if v > 1}
                if doubled:
                    say(f"!! DOUBLE FLATTEN: {doubled} - the shared snapshot "
                        f"fix did not hold")
                    return 2
                say("one flatten per underlying, as intended")
                return 0
            time.sleep(args.poll_seconds)
        except AlpacaCliError as exc:
            say(f"!! broker error while watching: {exc}")
            return 3
        except KeyboardInterrupt:
            say("stopped by hand")
            return 130

    say(f"!! backstop reached after {args.max_polls} polls with "
        f"{sorted(watched)} still held - this is NOT a clean result, the "
        f"gate should have flattened them in the first minutes of the "
        f"session")
    return 4


if __name__ == "__main__":
    sys.exit(main())
