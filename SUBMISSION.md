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

# Kangaroo Options — one-page write-up

**Alpaca AI Trading Agents Hackathon** · paper account **PA3S85G7JUS0**
· starting balance 100,000 USD · live sheet:
<https://hanky27.github.io/KangarooOptions/>

An autonomous options grid running 25 instruments — 15 underlyings, most
of them long *and* short — as one process against Alpaca's CLI. It sells
credit spreads, adds to a position that moves against it, and takes
profit on the cluster as a whole. There is no entry signal: the grid is
always in, and the only judgement in the system is about **how much**.

## The strategy, in one paragraph

Each instrument is a *cluster*. A cluster opens by selling one credit
spread — a put spread on the long side, a call spread on the short side,
short strike nearest the spot, wing at the first width that exists as a
real strike. When the underlying moves against it by `rebuy_1st_pct`
(1.2 %), it sells another spread further out; every further step needs
only `rebuy_pct` (0.10 %), and each leg is 1.1× the last. The cluster
closes when the *aggregate* mark of all its legs reaches the take-profit
cash, not when any single leg does — a losing first leg is carried by the
credit of the ones behind it. That is a martingale, deliberately: the
edge is not prediction, it is that a mean-reverting underlying only has
to come back part of the way for the whole cluster to close green.

## The AI logic

The grid needs no opinion about direction, and giving a model one would
have been theatre. It was given the decision the code structurally
**cannot** make.

`KangarooCore` sees its own legs and nothing else. What kills a
martingale grid is never one bad instrument — it is many instruments deep
at the same time, each correctly following its own rule, together
consuming the margin any one of them would have needed to recover. That
is a portfolio judgement across 25 state machines that are blind to each
other.

So on every rebuy that matters, the model is handed the **whole account
at once** — equity, cash, options buying power, maintenance margin, the
headroom between them, and every open cluster described in identical
terms and sorted by how far the underlying has run against it — and asked
one question: may this cluster add the size it asked for, less, or none?

It is asked only when the answer could matter: from the 4th leg of a
cluster onward, or whenever options buying power falls below 40 % of
equity. Routine rebuys never reach it. The poll loop stays at four
requests plus one account read, and the token bill stays proportional to
the decisions that carry weight.

## The risk gates

There are three, and they are deliberately of different kinds.

**1. A constant.** `max_adverse_pct` (5 %) stops a cluster adding once
the underlying has run that far against its anchor. Nothing is closed —
the cluster keeps its positions and its take-profit test. A grid that
stops adding survives; a grid forced to reduce realises its loss.

**2. A structure.** Every position is a *spread*, never a naked short.
The wing is bought in the same multi-leg order as the short leg, so the
maximum loss of each leg is bounded at entry by the width minus the
credit, and the account can never be assigned into an unhedged position.
Two further guards sit on top: a rebuy is refused if its short strike is
a contract the account already holds long, and an assignment gate flattens
assigned stock before any settlement is booked.

**3. A model, and it can only take risk away.** The gate returns an
integer between zero and the quantity the grid already asked for.
**Nothing else — and that is enforced in code, not in the prompt.** A
model answering 50 to a request of 2 gets 2, and the violation is counted
as a broken contract rather than honoured as a judgement. A model
answering prose, nothing, or a negative number leaves the request
untouched. There is no path through `risk_gate.py` by which a model opens
a position, enlarges one, chooses a strike, or moves a stop. The worst a
hallucinating or compromised model can do is stop the grid from adding —
exactly what the constant in gate 1 already does.

It fails **open**, loudly. No key, a timeout, a refusal, a malformed
answer: each is logged with its reason, counted, and the grid proceeds
with the size it asked for. Failing closed would turn a model outage into
a silent, untested strategy change while live positions are open. The
counters keep *"the gate agreed"* and *"the gate was down"*
distinguishable after the fact.

21 of the project's 83 tests cover this file alone, and every one of them
assumes the model misbehaves.

> **Status, honestly:** the gate ships `enabled: false` and is switched on
> per config once an API key is installed on the host. Turning it on
> changes what a running grid does with live positions, so it is a dated
> decision rather than a default. The startup banner says which of the two
> a given run was, and so do the counters.

## The Alpaca implementation

**The CLI is the only broker interface** — `alpaca`, pinned to v0.0.13.
Not a Python SDK, not raw HTTP: one binary, structured JSON out, which is
what makes the agent auditable from a log file. Positions, clock, quotes,
option chains, orders, fills, portfolio history and account activities
all come back through the same wrapper.

- **Multi-leg limit orders** (`order_class: mleg`) with a **negative
  limit price**, which is how a credit is expressed: `-1.59` means *pay
  me at least 1.59*. Both legs fill together or neither does.
- **One poll, four requests, any number of instruments.** Stock and
  option quotes are batched across all 25 instruments, so poll cost is
  constant in the instrument count rather than linear.
- **The broker is the authority on state, not the log.** Every order
  carries a `client_order_id` of
  `kang_<INSTRUMENT>_c<cluster>_l<leg>_<kind>_<ms>`. On startup the agent
  asks the broker what actually happened and recovers a close that filled
  while it was being stopped — a failure that cost three phantom clusters
  before it was understood.
- **A single-instance lock at OS level** (`msvcrt.locking` / `flock`),
  because a PID file survives a power cut and a kernel lock does not.
- **The live sheet reads back through the same CLI.** Realized P&L is
  rebuilt fill by fill at average cost, fees are summed from the
  account's own `FEE` bookings, and `equity − start = realized +
  unrealized − fees + transfers` must hold **to the cent** or the page is
  not published at all.

Runs as a service on a dedicated VPS with a two-minute watchdog.

## What is honest about the result

The grid parameters are inherited from the author's cTrader Forex bot and
are **not** tuned for options or for a one-week window. The backtest
window (Alpaca option data begins 2024-02) contains no sustained bear
market, so the tail this strategy is exposed to is bounded by
construction but unmeasured. Six defects were found and fixed while it
traded live; each one is in [DEVLOG.md](DEVLOG.md) with the measurement
that found it, the cause, and the regression test that now covers it.

Two of those six were not in the agent at all — they were in what the
project *said* about itself. One put a maximum drawdown of −102,137.75 on
the public page that had never happened. Finding that mattered more than
any single trade.
