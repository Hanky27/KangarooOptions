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

"""Publish the Alpaca bar downloads into the QuantroTrader cache.

Converts data/<sym>_daily.json / data/<sym>_hourly.json (Alpaca CLI
downloads, see fetch_alpaca_bars.py) into the provider cache layout the
QuantroTrader chart reader serves:

    <BacktestingCache>/Alpaca/<SYM>/stock/<SYM>_<YYYY>.parquet   (daily)
    <BacktestingCache>/Alpaca/<SYM>/h1/<SYM>_<YYYY>.parquet     (hourly)

Daily schema mirrors the ThetaData stock store exactly (columns date, open,
high, low, close, volume; date = naive YYYY-MM-DD trading day - Alpaca daily
bar timestamps are midnight America/New_York, so t[:10] IS the trading day).
Hourly rows keep the full UTC timestamp in a `timestamp` column instead of
`date`. A sidecar provenance file per store records the source file's
SHA-256, row count and coverage (QuantroTrader PROVENANCE-GATE convention).

Atomic writes (tmp + os.replace). Existing year files are OVERWRITTEN from
the current source json - the json is the source of truth of this store.

Usage:
  python alpaca_bars_to_cache.py SPY QQQ            # daily stores
  python alpaca_bars_to_cache.py --tf h1 SPY QQQ    # hourly stores
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE_ROOT = Path("D:/GoogleDrive/ShareFile/BacktestingCache")


def publish(symbol: str, tf: str = "d1") -> None:
    if tf == "d1":
        src = HERE / "data" / f"{symbol.lower()}_daily.json"
        subdir, key_col = "stock", "date"
    elif tf == "h1":
        src = HERE / "data" / f"{symbol.lower()}_hourly.json"
        subdir, key_col = "h1", "timestamp"
    else:
        raise ValueError(f"unsupported tf '{tf}' - d1 or h1")
    if not src.is_file():
        raise FileNotFoundError(f"bar store not found: {src}")
    body = json.loads(src.read_text(encoding="utf-8"))
    bars = body["bars"]
    if body.get("next_page_token"):
        raise RuntimeError(f"{src}: paginated download - store is incomplete")
    keys = ([b["t"][:10] for b in bars] if tf == "d1"
            else [b["t"] for b in bars])
    df = pd.DataFrame({
        key_col: keys,
        "open": [float(b["o"]) for b in bars],
        "high": [float(b["h"]) for b in bars],
        "low": [float(b["l"]) for b in bars],
        "close": [float(b["c"]) for b in bars],
        "volume": [int(b["v"]) for b in bars],
    })
    if df[key_col].duplicated().any():
        raise RuntimeError(f"{src}: duplicate {key_col} rows in source")

    out_dir = CACHE_ROOT / "Alpaca" / symbol / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    years = sorted({k[:4] for k in df[key_col]})
    for year in years:
        part = df[df[key_col].str.startswith(year)].reset_index(drop=True)
        path = out_dir / f"{symbol}_{year}.parquet"
        tmp = out_dir / f"{symbol}_{year}.parquet.tmp"
        part.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    sidecar = {
        "provider": "Alpaca",
        "symbol": symbol,
        "timeframe": tf,
        "source_file": str(src),
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "rows": int(len(df)),
        "first": df[key_col].min(),
        "last": df[key_col].max(),
        "created_by": "KangarooOptions/alpaca_bars_to_cache.py",
    }
    sc_path = out_dir / "provenance.json"
    sc_tmp = out_dir / "provenance.json.tmp"
    sc_tmp.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    os.replace(sc_tmp, sc_path)
    print(f"{symbol} {tf}: {len(df)} rows {sidecar['first']}..{sidecar['last']} "
          f"-> {out_dir} ({len(years)} year files + provenance.json)")


if __name__ == "__main__":
    argv = sys.argv[1:]
    tf = "d1"
    if argv and argv[0] == "--tf":
        if len(argv) < 3:
            print("usage: alpaca_bars_to_cache.py [--tf d1|h1] SYMBOL ...",
                  file=sys.stderr)
            sys.exit(2)
        tf, argv = argv[1], argv[2:]
    if not argv:
        print("usage: alpaca_bars_to_cache.py [--tf d1|h1] SYMBOL ...",
              file=sys.stderr)
        sys.exit(2)
    for s in argv:
        publish(s.upper(), tf)
