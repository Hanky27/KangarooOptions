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

"""Thin wrapper around the official Alpaca CLI (github.com/alpacahq/cli).

The hackathon requires order execution through Alpaca's MCP server or CLI;
this project uses the CLI. Every call shells out to `alpaca ... -q` and
parses the JSON from stdout.

Fail-loud policy: any non-zero exit code, any JSON body carrying an "error"
key, unparseable output, or a paginated contract listing raises
AlpacaCliError immediately. There are no retries and no fallbacks.

Paper-only guard (single place): construction refuses to run when
ALPACA_LIVE_TRADE is set truthy in the environment. With API keys from the
environment the CLI defaults to paper trading.
"""

from __future__ import annotations

import json
import os
import subprocess


class AlpacaCliError(RuntimeError):
    pass


# The most symbols one latest-quotes request accepts. Not a guess: the
# endpoint answers `{"error": "symbol limit is 100", "status": 400}` and
# returns NOTHING when the list is longer, which is how the agent died on
# 2026-09-01 with 102 open legs.
# The broker's option lifecycle bookings: the option leaves the account and
# stock takes its place. Measured 2026-09-03 over all 21 such rows on the
# competition account: net_amount is exactly "0" on every one of them. The
# CASH of an assignment moves in a separate OPTRD row - a stock trade at the
# strike, e.g. {"symbol": "GLD", "qty": "100", "price": "405",
# "net_amount": "-40500"} - which is why these three may never be counted as
# a fee or a transfer, and why the OPTRD rows must be.
OPTION_SETTLEMENT_TYPES = frozenset({"OPEXP", "OPASN", "OPEXC"})
OPTION_STOCK_TRADE_TYPE = "OPTRD"

QUOTE_SYMBOL_LIMIT = 100

# The most orders one `order list` response can carry. Not a choice: the
# CLI's own help says "Defaults to 50 and max is 500", and asking for more
# returns 500 with no indication that anything was left behind.
PAGE_LIMIT = 500


def in_batches(symbols: list[str], size: int = QUOTE_SYMBOL_LIMIT):
    """Split a symbol list into request-sized pieces, order preserved."""
    for start in range(0, len(symbols), size):
        yield symbols[start:start + size]


class AlpacaCli:
    def __init__(self, cli_path: str, env_file: str | None = None) -> None:
        if not os.path.isfile(cli_path):
            raise AlpacaCliError(f"Alpaca CLI not found: {cli_path}")
        self._cli_path = cli_path
        self._env = os.environ.copy()
        if env_file:
            self._load_keys_from_file(env_file)
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            if not self._env.get(key):
                raise AlpacaCliError(
                    f"{key} is not set (environment or env_file) - refusing to start")
        # Paper-only guard - the ONE place that decides this.
        if self._env.get("ALPACA_LIVE_TRADE", "").lower() in ("true", "1", "yes"):
            raise AlpacaCliError(
                "ALPACA_LIVE_TRADE is set - this agent is paper-only by design")

    def _load_keys_from_file(self, env_file: str) -> None:
        """Read ALPACA_API_KEY / ALPACA_SECRET_KEY lines from a KEY=VALUE
        file. Only these two keys are imported - nothing else leaks in."""
        if not os.path.isfile(env_file):
            raise AlpacaCliError(f"env_file not found: {env_file}")
        with open(env_file, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("ALPACA_API_KEY=") or line.startswith("ALPACA_SECRET_KEY="):
                    key, _, value = line.partition("=")
                    self._env[key] = value

    # --- plumbing --------------------------------------------------------

    def _run(self, args: list[str]):
        proc = subprocess.run(
            [self._cli_path, *args, "-q"],
            capture_output=True, text=True, env=self._env,
        )
        out = proc.stdout.strip()
        if proc.returncode != 0:
            raise AlpacaCliError(
                f"alpaca {' '.join(args)} failed (exit {proc.returncode}):\n"
                f"{out}\n{proc.stderr.strip()}")
        if not out:
            return None
        try:
            body = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AlpacaCliError(
                f"alpaca {' '.join(args)}: unparseable output: {out[:500]}") from exc
        if isinstance(body, dict) and body.get("error"):
            raise AlpacaCliError(f"alpaca {' '.join(args)}: {body['error']}")
        return body

    # --- read endpoints --------------------------------------------------

    def clock(self) -> dict:
        return self._run(["clock"])

    def account(self) -> dict:
        return self._run(["account", "get"])

    def stock_quote(self, symbol: str) -> dict:
        """Latest NBBO quote of the underlying: {'ap','bp','t',...}."""
        body = self._run(["data", "latest-quote", "--symbol", symbol])
        return body["quote"]

    def stock_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Latest NBBO quotes for several underlyings, batched:
        {symbol: {'ap','bp','t',...}}.

        The single-symbol endpoint costs one request per symbol, which
        makes the poll rate scale with the number of instruments. This one
        costs one request per QUOTE_SYMBOL_LIMIT symbols instead - a
        ceiling the endpoint enforces with a 400 and no partial result.
        """
        quotes: dict[str, dict] = {}
        for batch in in_batches(symbols):
            body = self._run(["data", "latest-quotes",
                              "--symbols", ",".join(batch)])
            quotes.update(body["quotes"])
        return quotes

    def option_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Latest quotes for OCC option symbols, batched the same way.

        This is the one that broke: the grid crossed 100 open legs on
        2026-09-01 and every poll from then on asked for 102 symbols at
        once. The endpoint answered `symbol limit is 100` with status 400
        and no quotes at all, so market_data() raised before the first
        instrument was stepped and 25 grids went unmanaged for 93 minutes.
        """
        quotes: dict[str, dict] = {}
        for batch in in_batches(symbols):
            body = self._run(["data", "option", "latest-quotes",
                              "--symbols", ",".join(batch)])
            quotes.update(body["quotes"])
        return quotes

    def option_contracts(self, underlying: str, exp_gte: str, exp_lte: str,
                         type_: str) -> list[dict]:
        """Active contracts of one underlying within an expiration window.
        A non-empty next_page_token would silently truncate the universe,
        so it is an error."""
        body = self._run([
            "option", "contracts",
            "--underlying-symbols", underlying,
            "--expiration-date-gte", exp_gte,
            "--expiration-date-lte", exp_lte,
            "--type", type_,
            "--limit", "10000",
        ])
        if body.get("next_page_token"):
            raise AlpacaCliError(
                "option contracts listing is paginated - result would be incomplete")
        return body["option_contracts"]

    def daily_bars(self, symbol: str, start: str, end: str) -> list[dict]:
        """Split-adjusted daily bars of one stock/ETF, inclusive both ends.

        The agent settles an expired leg against the underlying's close on
        the expiry day, so this runs in the LIVE path. It belongs here and
        not in fetch_alpaca_bars.py: that module carries its own module
        level CLI_PATH and ENV_FILE pointing at the author's workstation
        (`C:/Users/HMz/Documents/Source/...`), which do not exist on the
        trading host. Measured 2026-09-03 on VPS2: every settlement raised
        FileNotFoundError on that env.txt, IWM_short was halted at startup
        and AAPL_short after 20 consecutive failed polls - 4 of 25
        instruments dead with the market about to open. This method takes
        its path and its keys from the SAME config-driven AlpacaCli every
        order already goes through, so there is one place that knows where
        the binary and the keys are.

        Paged to exhaustion on next_page_token; a page that errors raises.
        """
        bars: list[dict] = []
        token: str | None = None
        while True:
            cmd = ["data", "bars", "--symbol", symbol,
                   "--timeframe", "1Day", "--start", start, "--end", end,
                   "--adjustment", "split", "--limit", "10000"]
            if token:
                cmd += ["--page-token", token]
            body = self._run(cmd)
            bars += (body.get("bars") or []) if body else []
            token = (body or {}).get("next_page_token") or None
            if not token:
                return bars

    def positions(self) -> list[dict]:
        body = self._run(["position", "list"])
        return body if body else []

    def orders_since(self, iso_ts: str, page: int = PAGE_LIMIT,
                     max_pages: int = 40) -> list[dict]:
        """Every order submitted after `iso_ts`, any status, ALL of them.

        Used at startup to ask the broker what really happened to a close
        the agent may not have lived long enough to book. There is no
        lookup by client_order_id in the CLI (verified against
        `order list --help`), so the window is fetched and filtered here.

        Paged, because the endpoint caps a response at 500 no matter what
        is asked for - its help says so: "maximum number of orders in
        response. Defaults to 50 and max is 500". The previous version
        asked for 1000 and checked the answer against 1000, so a full page
        of 500 sailed past the check and the caller got a truncated list
        that reported success. Measured 2026-09-01: the list began at
        15:20:41 UTC on a day whose first fill was at 13:40, and nothing
        said so.

        `--direction asc` with `--after-order-id` walks forward from the
        oldest. A short page is the end. Running past `max_pages` raises
        rather than returning what happens to have been collected: this
        list decides whether a live cluster is kept or booked.
        """
        out: list[dict] = []
        after_id: str | None = None
        for _ in range(max_pages):
            cmd = ["order", "list", "--status", "all", "--after", iso_ts,
                   "--direction", "asc", "--limit", str(page)]
            if after_id:
                cmd += ["--after-order-id", after_id]
            body = self._run(cmd)
            rows = body if isinstance(body, list) else (body.get("orders") or [])
            out += rows
            if len(rows) < page:
                return out
            after_id = rows[-1]["id"]
        raise AlpacaCliError(
            f"orders_since({iso_ts}) still had pages after {max_pages} x "
            f"{page} orders - refusing to decide a cluster's fate on a "
            f"list that may be incomplete")

    def activities(self, after: str, category: str = "non_trade_activity",
                   page_size: int = 100) -> list[dict]:
        """Every account booking of `category` created on or after `after`.

        This is where fees, dividends, assignments and cash transfers
        live - everything that moves cash without a fill. The report
        reconciles against these instead of assuming a fee schedule,
        because a rate that is right today is a wrong number tomorrow.

        Paged to exhaustion. `--page-token` takes the id of the last row
        of the previous page (verified against
        `account activity list --help`), and a short page is the end of
        the list.

        The order is the BROKER'S, not booking order: `--direction asc`
        sorts by the activity's own date, and the id it pages on carries
        only a day stamp (`20260831000000000::<uuid>`), so rows of one day
        come back in uuid order. Measured over 299 rows: first
        created_at 18:29:19Z, last 15:19:08Z. A caller that needs a
        running total in time order has to sort on `created_at` itself.
        """
        rows: list[dict] = []
        token: str | None = None
        while True:
            cmd = ["account", "activity", "list",
                   "--category", category,
                   "--after", after,
                   "--page-size", str(page_size),
                   "--direction", "asc"]
            if token:
                cmd += ["--page-token", token]
            body = self._run(cmd)
            page = body if isinstance(body, list) else (
                body.get("activities") or body.get("data") or [])
            if not page:
                break
            rows += page
            if len(page) < page_size:
                break
            token = page[-1]["id"]
        return rows

    def order_get(self, order_id: str) -> dict:
        return self._run(["order", "get", "--order-id", order_id])

    # --- write endpoints -------------------------------------------------

    def submit_option_market(self, symbol: str, qty: int, side: str,
                             position_intent: str, client_order_id: str,
                             dry_run: bool = False) -> dict:
        args = [
            "order", "submit",
            "--symbol", symbol,
            "--qty", str(qty),
            "--side", side,
            "--type", "market",
            "--time-in-force", "day",
            "--position-intent", position_intent,
            "--client-order-id", client_order_id,
        ]
        if dry_run:
            args.append("--dry-run")
        return self._run(args)

    def submit_mleg_limit(self, legs: list[dict], qty: int, limit_price: float,
                          client_order_id: str, dry_run: bool = False) -> dict:
        """Multi-leg LIMIT day order. Alpaca convention: negative limit_price
        = minimum net CREDIT, positive = maximum net debit (order form
        verified with a live paper order on 2026-08-28). Each leg dict:
        {symbol, ratio_qty (string), side, position_intent}."""
        args = [
            "order", "submit",
            "--order-class", "mleg",
            "--qty", str(qty),
            "--type", "limit",
            "--limit-price", str(limit_price),
            "--time-in-force", "day",
            "--client-order-id", client_order_id,
            "--legs", json.dumps(legs),
        ]
        if dry_run:
            args.append("--dry-run")
        return self._run(args)

    def submit_equity_market(self, symbol: str, qty: float, side: str,
                             client_order_id: str) -> dict:
        """Market day order on the underlying - used ONLY by the assignment
        gate (an assigned stock position is flattened immediately)."""
        return self._run([
            "order", "submit",
            "--symbol", symbol,
            "--qty", str(qty),
            "--side", side,
            "--type", "market",
            "--time-in-force", "day",
            "--client-order-id", client_order_id,
        ])

    def cancel_order(self, order_id: str) -> None:
        """Cancel ONE order by id. There is deliberately no cancel-all here:
        it would also kill orders this agent does not own."""
        self._run(["order", "cancel", "--order-id", order_id])
