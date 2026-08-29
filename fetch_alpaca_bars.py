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

"""Fetch historical stock bars via the Alpaca CLI into a local JSON store.

Pages through the CLI's 10,000-row limit with --page-token and merges every
page into ONE bars list (fail-loud on any CLI error). Timestamps stay
exactly as the API returns them (UTC).

Usage:
  python fetch_alpaca_bars.py SPY 1Hour 2016-01-01 2026-08-27 data/spy_hourly.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI_PATH = "C:/Users/HMz/Documents/Source/AlpacaTools/cli/alpaca.exe"
ENV_FILE = "C:/Users/HMz/Documents/Source/McpServer/alpaca-mcp-server/dist/env.txt"


def _cli_env() -> dict:
    env = os.environ.copy()
    with open(ENV_FILE, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("ALPACA_API_KEY=") or line.startswith("ALPACA_SECRET_KEY="):
                key, _, value = line.partition("=")
                env[key] = value
    return env


def fetch(symbol: str, timeframe: str, start: str, end: str) -> list[dict]:
    env = _cli_env()
    bars: list[dict] = []
    token = None
    page = 0
    while True:
        args = [CLI_PATH, "data", "bars", "--symbol", symbol,
                "--timeframe", timeframe, "--start", start, "--end", end,
                "--adjustment", "split", "--limit", "10000", "-q"]
        if token:
            args += ["--page-token", token]
        proc = subprocess.run(args, capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"alpaca data bars failed (page {page}): "
                               f"{proc.stdout.strip()} {proc.stderr.strip()}")
        body = json.loads(proc.stdout)
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"alpaca data bars page {page}: {body['error']}")
        chunk = body.get("bars") or []
        bars.extend(chunk)
        token = body.get("next_page_token") or None
        page += 1
        print(f"page {page}: +{len(chunk)} bars (total {len(bars)})", flush=True)
        if not token:
            return bars


def main() -> int:
    if len(sys.argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    symbol, timeframe, start, end, out = sys.argv[1:]
    bars = fetch(symbol.upper(), timeframe, start, end)
    if not bars:
        raise RuntimeError(f"no bars returned for {symbol} {timeframe} "
                           f"{start}..{end}")
    out_path = HERE / out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"symbol": symbol.upper(), "timeframe": timeframe,
                   "bars": bars, "next_page_token": ""}, fh)
    os.replace(tmp, out_path)
    print(f"{symbol} {timeframe}: {len(bars)} bars "
          f"{bars[0]['t']} .. {bars[-1]['t']} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
