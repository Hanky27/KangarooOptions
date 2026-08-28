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

An anti-martingale grid trading agent on US options, built for the
**Alpaca AI Trading Agents Hackathon** (lablab.ai, Aug 28 - Sep 4, 2026).
It is a deliberately stripped-down port of the author's cTrader
"Kangaroo" V2.2 Forex grid bot.

## Strategy

Kangaroo is a pure grid - there is no entry signal:

1. Open an initial leg immediately.
2. Whenever the **underlying** moves adversely by `rebuy_1st_pct`
   (first rebuy) or `rebuy_pct` (all further rebuys) from the last leg's
   entry reference, add a new leg with a growing size (`1.1^n`).
3. Close the whole cluster as soon as its aggregated option P&L exceeds
   `invest_count * initial_qty * 100 * (tp_pct% of the underlying price)`.
4. **Mode1 toggle:** after every profitable cluster close the direction
   flips - a CALL cluster becomes a PUT cluster and vice versa.

Direction is expressed purely by contract type:

| Cluster | Leg order |
|---|---|
| Long (CALL cluster) | buy-to-open **call**, strike nearest spot, nearest expiry in the DTE window |
| Short (PUT cluster) | buy-to-open **put**, same selection |

Both directions consist exclusively of buy-to-open orders - there is no
short selling anywhere in this variant. Trigger math always runs on the
underlying quote, never on option premiums.

Deliberately **not** ported from the original: Mode2/Mode3, Freeze/Unfreeze,
hedging/netting order modes, grid close, PID factors, multi-symbol support,
FX pip/spread simulation. This repo serves exactly one purpose.

## Architecture

| File | Role |
|---|---|
| `kangaroo_core.py` | Pure state machine + math. No I/O. |
| `alpaca_cli.py` | Thin fail-loud wrapper around the official [Alpaca CLI](https://github.com/alpacahq/cli). |
| `agent.py` | Poll loop: clock -> underlying quote -> close check -> rebuy check. |
| `test_kangaroo_core.py` | Self-tests of the core (`python test_kangaroo_core.py`). |

All broker access goes through **Alpaca's CLI** (hackathon requirement:
MCP server or CLI - no raw API calls). Orders are market/day with unique
`client_order_id`s; fills are awaited by polling the order status. Any CLI
error, missing quote, or inconsistent state stops the agent immediately -
no retries, no fallbacks.

## Safety

- **Paper-only:** the agent refuses to start when `ALPACA_LIVE_TRADE` is
  set. With plain API keys the Alpaca CLI defaults to paper trading.
- **No cancel-all:** only ID-based order handling.
- **Crash-safe state:** cluster state is persisted atomically to
  `state_file` and reconciled against the account's real positions at
  startup; any mismatch aborts.

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
- Long options pay theta while a cluster is open; `tp_pct` is therefore a
  tuning parameter, not a like-for-like carryover from FX.
- Strike/DTE selection is static (ATM, nearest expiry in window) - the
  planned AI layer (underlying choice, DTE/strike policy, risk gates) is
  not part of this core yet.
