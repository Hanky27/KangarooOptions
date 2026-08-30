<!--
MIT License
Copyright (c) 2026 Heinrich Munz

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
-->

# Kangaroo Options

A martingale-style grid trading agent on US options, built for the
**Alpaca AI Trading Agents Hackathon** (lablab.ai, Aug 28 - Sep 4, 2026).
It is a deliberately stripped-down port of the author's cTrader
"Kangaroo" V2.2 Forex grid bot.

## Strategy

Kangaroo is a pure grid - there is no entry signal. This port sells
**put credit spreads** instead of trading the underlying:

1. Sell an initial spread immediately: SHORT the ATM put, LONG a
   protective put ~5 $ lower (one multi-leg limit order, net credit).
2. Whenever the **underlying** drops by `rebuy_1st_pct` (first rebuy) or
   `rebuy_pct` (all further rebuys) from the last leg's entry reference,
   sell another spread with a growing size (`1.1^n`).
3. Stop rebuying - but close nothing - once the underlying has run
   `max_adverse_pct` against the cluster's FIRST leg (0 disables it). The
   cluster keeps every position and still waits for its take profit; it
   just stops adding to a move that has already gone against it.
4. Close the whole cluster (buy back every spread) as soon as the WHOLE
   cluster - open legs plus the realized pot of already-settled ones -
   exceeds `invest_count * initial_qty * 100 * (tp_pct% of the underlying
   price)`. Because the threshold is positive, a cluster never takes
   profit while its total is negative.
5. **Expiry = let legs expire:** a leg reaching expiration is settled by
   the broker at the underlying's close of that day, and its realized USD
   joins the cluster's sunk pot. OTM expiry keeps the full credit. The
   cluster keeps living with its remaining legs.
6. **Put-only:** after a cluster ends the grid restarts in the same
   direction - there is no Mode1 toggle.

Trigger math always runs on the underlying quote, never on option premiums.

The configuration is the winner of an 11-run backtest sweep (SPY,
2024-02..2026-08, real Alpaca option prices, daily resolution): the
put-credit-spread grid was the only account-sized profitable variant, while
every long-option variant of the same grid was negative and the Mode1 short
side lost money in every measured style (see `backtest_options.py`; window
caveat: no extended bear market in the data, marks are trade closes without
spread costs).

**Corrected 2026-08-29.** The simulator refreshed the protective wing's mark
only for CALL spreads, so every PUT spread was valued against a wing frozen
at its entry price. Differential test (one line changed, same data): the
call-only run is bit-identical, the put-only run drops from **+34,484 USD**
to **+2,688 USD** (max drawdown -2,721 -> -2,954 USD, margin peak unchanged
at 2,000 USD). All put-spread figures published before that date are
inflated; the parameter set has not been re-tuned against the corrected
numbers yet.

Deliberately **not** ported from the original: Mode1/Mode2/Mode3,
Freeze/Unfreeze, hedging/netting order modes, grid close, PID factors,
multi-symbol support, FX pip/spread simulation. This repo serves exactly
one purpose.

## Architecture

| File | Role |
|---|---|
| `kangaroo_core.py` | Pure state machine + math. No I/O. |
| `alpaca_cli.py` | Thin fail-loud wrapper around the official [Alpaca CLI](https://github.com/alpacahq/cli). |
| `agent.py` | Poll loop: expiry/assignment gates -> clock -> underlying quote -> close check -> rebuy check. |
| `backtest_underlying.py` | Stage-1 edge check on the underlying itself (upper bound). |
| `backtest_options.py` | Stage-2 backtests with real Alpaca option prices (3 styles, 4 regimes). |
| `test_kangaroo_core.py` | Self-tests of the core (`python test_kangaroo_core.py`). |

All broker access goes through **Alpaca's CLI** (hackathon requirement:
MCP server or CLI - no raw API calls). Spreads are multi-leg LIMIT day
orders at marketable net-credit/net-debit limits with unique
`client_order_id`s; fills are awaited by polling the order status, and an
unfilled order is canceled by ID and re-quoted on the next loop. Any CLI
error, missing quote, or inconsistent state stops the agent immediately -
no retries, no fallbacks.

## Safety

- **Paper-only:** the agent refuses to start when `ALPACA_LIVE_TRADE` is
  set. With plain API keys the Alpaca CLI defaults to paper trading.
- **Defined risk per leg:** the wing caps every spread's loss at
  `(width - credit) * 100` per contract; margin per leg is the spread
  width, not the strike.
- **Assignment gate:** an assigned stock position in the underlying is
  flattened immediately (no wheel).
- **No cancel-all:** only ID-based order handling.
- **Crash-safe state:** cluster state is persisted atomically to
  `state_file` and reconciled against the account's real positions
  (short leg AND wing) at startup; any mismatch aborts.

## Setup

1. Install the [Alpaca CLI](https://github.com/alpacahq/cli) (Windows:
   download the release zip; or `go install github.com/alpacahq/cli/cmd/alpaca@latest`).
2. `pip install pyyaml`
3. `copy config.example.yaml config.yaml` and set `cli_path`
   (plus `env_file`, or export `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`).
4. Self-tests: `python test_kangaroo_core.py`
5. Single decision pass without any order: `python agent.py --once --dry-run`
6. Run: `python agent.py`

## Status / open points

- Grid parameters are carried over from the FX original (AUDCAD, H1) and
  are **not yet tuned** for options or the one-week contest window.
- The backtest window (Alpaca option data starts 2024-02) contains no
  extended bear market; a sustained downtrend makes the put grid lose the
  (capped) spread width repeatedly. This tail is bounded by construction
  but unmeasured.
- Strike/DTE/width selection is static (ATM, nearest expiry in window,
  first available width) - the planned AI layer (underlying choice,
  DTE/strike policy, risk gates) is not part of this core yet.
