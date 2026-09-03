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

"""Count what the risk gate has actually done, out of the agent's own logs.

The write-up says the model reduced 31 requests, refused 13 outright and
let 5 through, and that it failed twice and passed the request on both
times. Those are the load-bearing numbers of the whole submission, so they
should not be a sentence someone typed - they should be a command anyone
can run against the log files and get the same answer.

That is what this is. It parses the two shapes the agent writes:

    <INSTRUMENT>: RISK GATE [model] <asked> -> <got> (<ms> ms): <reason>
    <INSTRUMENT>: RISK GATE [error] <asked> -> <asked> (<ms> ms): <what broke>

and reports the distribution, the contract totals, the latency range and
every failure. `[error]` lines are the fail-open property in the evidence:
the second number equals the first, so the grid's own request went
through untouched.

Usage:
    python tools_gate_report.py --logs logs
    python tools_gate_report.py --logs logs --tail 8     # for the camera
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The agent writes "NAME: RISK GATE [kind] a -> b (n ms): reason", with an
# ISO stamp in front of it. Anchored on the arrow so a reason containing
# "->" cannot be mistaken for the decision itself.
LINE = re.compile(
    r"^\[?(?P<ts>[0-9T:.\-]+Z?)?\]?\s*"
    r"(?P<inst>[A-Z]+_[a-z]+):\s+RISK GATE\s+\[(?P<kind>model|error)\]\s+"
    r"(?P<asked>\d+)\s*->\s*(?P<got>\d+)\s+\((?P<ms>\d+)\s*ms\):\s*"
    r"(?P<why>.*)$")


def parse(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = LINE.match(line.strip())
                if not m:
                    continue
                d = m.groupdict()
                rows.append({
                    "file": os.path.basename(path),
                    "ts": d["ts"] or "",
                    "instrument": d["inst"],
                    "kind": d["kind"],
                    "asked": int(d["asked"]),
                    "got": int(d["got"]),
                    "ms": int(d["ms"]),
                    "why": d["why"].strip(),
                })
    rows.sort(key=lambda r: (r["ts"], r["file"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default=os.path.join(HERE, "logs"),
                    help="directory of agent_YYYY-MM-DD.log files")
    ap.add_argument("--tail", type=int, default=0,
                    help="also print the last N decisions in full")
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.logs, "agent_*.log")))
    if not paths:
        print(f"no agent_*.log in {args.logs}", file=sys.stderr)
        return 2
    rows = parse(paths)
    if not rows:
        print(f"{len(paths)} log file(s), no RISK GATE decision in them - "
              f"the gate is off, or has not been consulted yet")
        return 0

    model = [r for r in rows if r["kind"] == "model"]
    errors = [r for r in rows if r["kind"] == "error"]
    unchanged = [r for r in model if r["got"] == r["asked"]]
    reduced = [r for r in model if 0 < r["got"] < r["asked"]]
    refused = [r for r in model if r["got"] == 0]
    asked = sum(r["asked"] for r in model)
    got = sum(r["got"] for r in model)
    ms = [r["ms"] for r in model]

    print(f"{len(paths)} log file(s), {rows[0]['ts'][:19]} .. "
          f"{rows[-1]['ts'][:19]}")
    print()
    print(f"  decisions by the model  {len(model):5d}")
    print(f"    left unchanged        {len(unchanged):5d}")
    print(f"    reduced               {len(reduced):5d}")
    print(f"    refused outright      {len(refused):5d}")
    print(f"  contracts asked for     {asked:5d}")
    print(f"  contracts allowed       {got:5d}")
    if asked:
        print(f"  size removed            {100.0 * (asked - got) / asked:4.0f} %")
    if ms:
        print(f"  latency ms              min {min(ms)}  max {max(ms)}  "
              f"mean {sum(ms) // len(ms)}")
    # A gate that can only subtract must never hand back more than it was
    # asked for. This is the clamp, checked against the log rather than
    # against the code that implements it.
    over = [r for r in model if r["got"] > r["asked"]]
    print(f"  answers above the request {len(over):3d}   "
          f"{'(the clamp holds)' if not over else '!! CLAMP BREACHED'}")

    print()
    print(f"  failures, passed through untouched  {len(errors)}")
    for r in errors:
        ok = "passed through" if r["got"] == r["asked"] else "!! CHANGED"
        print(f"    {r['ts'][:19]}  {r['instrument']:12} "
              f"{r['asked']} -> {r['got']}  {ok}  ({r['ms']} ms)  "
              f"{r['why'][:60]}")

    if args.tail:
        print()
        print(f"  last {min(args.tail, len(model))} decisions:")
        for r in model[-args.tail:]:
            print(f"    {r['ts'][:19]}  {r['instrument']:12} "
                  f"{r['asked']} -> {r['got']}  ({r['ms']} ms)")
            print(f"      {r['why']}")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
