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

## Hackathon submission

Alpaca AI Trading Agents Hackathon (lablab.ai), 28 August - 4 September
2026.

| | |
|---|---|
| Competition paper account | **PA3S85G7JUS0** |
| Account created | 2026-08-31, dedicated to this submission |
| Starting balance | 100,000 USD |
| Orders before the competition window | **none** - verified against the orders endpoint |
| Alpaca interface | the official **CLI** (`alpaca`), pinned to v0.0.13 |
| Options | credit spreads, multi-leg limit orders |
| Market data | Alpaca Basic (indicative options feed) |

### Development during the run

The hackathon is judged on the robustness of the agent workflow as well as
on P&L, so every change made to the agent WHILE it traded the competition
account is recorded in **[DEVLOG.md](DEVLOG.md)** - the symptom that was
measured, the cause with its evidence, the fix, and the measurement that
confirmed it. Two defects were found and fixed live on day one; both were
invisible to the backtest because they live in the execution path, which
the simulator does not exercise.

Deployment to the running agent goes through
`deploy/update_and_restart.ps1`, which waits for the observed change at
every step and refuses to report success unless the commit in the running
process's log banner is the one just shipped.

### Disclosure of pre-event work

The event FAQ permits work done before kick-off and requires it to be
disclosed. What predates the hackathon:

- **The strategy is a port.** Kangaroo is the author's own cTrader grid
  bot ("Kangaroo V2.2", Forex). Its rules - rebuy on adverse moves with
  `1.1^n` growth, cluster take-profit on aggregated P&L - were designed
  long before this event. What was built *for* the event is the options
  translation: credit spreads instead of spot, the expiry/settlement path,
  the assignment gate, the multi-instrument loader, and every measurement
  in this repository.
- **One commit predates the kick-off.** `f4e8e3f`, 2026-08-28 16:41 CEST -
  eighteen minutes before the 17:00 CEST start. It is the initial skeleton.
  Everything from `fb66ce7` onwards was written during the event.
- **The research harness is shared with the author's other work.** The
  linearity metric used by `tools_sweep.py` is imported from a private
  QuantroTrader module (`SignalEngine/optimizer/equity_shape.py`); the GUI
  the backtests are shown in is that same private application. Neither is
  part of this submission, and nothing in this repository requires them -
  `tools_week.py`, `tools_portfolio.py` and `backtest_options.py` run
  standalone.
- **No LLM is in the trading loop.** The agent is deterministic. This is
  stated plainly rather than dressed up: what is autonomous here is the
  full decision-and-execution cycle, not a language model.

## Strategy

Kangaroo is a pure grid - there is no entry signal. This port sells
**credit spreads** instead of trading the underlying. Each instrument runs
one direction, set by `start_long`:

| `start_long` | side | contract | profits while |
|---|---|---|---|
| `true` | long / bullish | **put** credit spread | the underlying holds or rises |
| `false` | short / bearish | **call** credit spread | the underlying holds or falls |

1. Sell an initial spread immediately: SHORT the ATM option, LONG a
   protective wing ~5 $ further out of the money - below the short strike
   for puts, above it for calls (one multi-leg limit order, net credit).
2. Whenever the **underlying** moves against the cluster by
   `rebuy_1st_pct` (first rebuy) or `rebuy_pct` (all further rebuys) from
   the last leg's entry reference, sell another spread with a growing size
   (`1.1^n`). Against means falling for a put grid, rising for a call
   grid.
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
6. **One direction per instrument:** after a cluster ends the grid
   restarts on the same side - there is no Mode1 toggle. A symbol that
   should be traded both ways gets two instrument configs.

Trigger math always runs on the underlying quote, never on option premiums.

## Instruments

One process drives N instruments, in the loader shape the author's cTrader
bots use. `config.yaml` is the **loader**: it holds the process-wide
settings (CLI path, credentials file, sampling rates) and the grid defaults
every instrument inherits. `config_path` names a folder; every `*.yaml` in
it is one instrument that overrides what it names.

    config.yaml            # loader: paths, poll rates, grid defaults
    configs/
      spy_short.yaml       # underlying + start_long + its own tuning
      qqq_short.yaml
      iwm_long.yaml
      ...

An instrument is identified by **(underlying, direction)**, so one symbol
may carry a long and a short grid at once: they hold different contracts
and never touch the same position. Each gets its own state file
(`state/kangaroo_<symbol>_<side>.json`) and its own `client_order_id`
namespace.

Refused loud, because each would silently corrupt a live grid:

- an instrument config that sets a process-wide key (`cli_path`,
  `env_file`, `poll_seconds`, `poll_fill_seconds`, `fill_requote_samples`,
  `config_path`) - those describe the process, not the grid;
- two configs for the same underlying **and** direction - they would fight
  over the same positions;
- a `state_file` in the loader while `config_path` names a folder - that
  one file would be shared by every instrument, so each would load the
  previous one's cluster and overwrite it on the next save.

Set `config_path: "_"` to fall back to a single instrument built from the
loader itself.

The clock and the position list are read **once per poll** for all
instruments, not once per instrument.

### Choosing the instruments

`tools_week.py` measures every candidate (DTE window x take-profit x
direction) per symbol over the last two trading weeks and reports **wins
per week** - the objective for a contest scored over a single week, where
a curve that needs months to straighten has no time to do so.
`tools_make_configs.py` turns those measurements into the files above,
admitting a side only if it is net positive over the window, and writes
the measurement into each config as a comment.

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
