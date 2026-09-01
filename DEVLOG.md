# Development log — Alpaca AI Trading Agents Hackathon

Every change made to the agent **while it was trading the competition
account**, in the order it happened, with the measurement that motivated it
and the measurement that confirmed it.

The hackathon judges "the creativity, autonomy, and robustness of the agent
trading workflow" alongside P&L, and asks that pre-event work be disclosed.
This file is the honest record of the second part: what broke live, how it
was found, and what proves it is fixed. Nothing here is reconstructed from
memory — every figure is copied from a command that ran.

Account: **PA3S85G7JUS0** · window: Mon 2026-08-31 09:30 ET →
Thu 2026-09-03 EOD (equity snapshot) · repo:
<https://github.com/Hanky27/KangarooOptions>

Deployment during the run goes through `deploy/update_and_restart.ps1`,
which stops the agent, waits for the **process** to disappear (a task
reporting "Ready" only means the scheduler let go), starts it again, waits
for a **new pid**, and then refuses to call the deployment done unless the
commit written into the log banner is the one just shipped. The agent
reloads every cluster from `state/kangaroo_<sym>_<side>.json` and
reconciles it against the account before it trades again, so a restart
mid-session is safe by construction.

---

## Before the open

### `3e00877` — the log had two encodings and could not be searched

**Symptom.** A pre-flight check for `grep ready` found **0 matches** in a
log file that contained 25 of them; `grep` called the file binary.

**Cause.** The banner was written with `Add-Content -Encoding UTF8` while
every agent line came through `Tee-Object`, whose default in Windows
PowerShell 5.1 is UTF-16. One file, two encodings.

**Fix.** One explicit UTF-8 `StreamWriter` with `AutoFlush` — flushed per
line, because a buffered writer loses the last and most interesting lines
exactly when a process is killed. Built on a `FileStream` with
`FileShare.ReadWrite`, after the first attempt locked out every reader and
made the live log untailable.

**Confirmed.** The same search now returns **50** matches; `Select-String`
and `Get-Content` both read the file while the agent writes it.

---

## During the trading session

### `f9b7bc0` — `client_order_id must be unique`

**Symptom** (09:39 ET). Six instruments — `DIA_short`, `TLT_long`,
`GLD_short`, `TSLA_long`, `NVDA_short`, `SPY_short` — failed **every**
poll and were walking towards their 20-strike halt without ever opening a
position. Counters observed climbing 1 → 7 over roughly two minutes.

**Cause.** The broker's answer, read out of the log:

```json
{ "code": 40010001,
  "error": "client_order_id must be unique",
  "status": 422 }
```

An unfilled limit order is cancelled by id after `fill_requote_samples`
polls and re-quoted on the next loop — that is by design. But `coid()`
built the id from instrument + cluster + leg + kind only, so the re-quote
submitted the **same id the broker had already seen**. Every retry
produced that same id again, so the instrument could never recover.

**Fix.** The id carries a millisecond stamp
(`kang_SPY_short_c1_l0_o_1788183674225`, 36 of the 128 characters the
broker allows). A per-process counter would have collided again after a
restart; a timestamp does not.

**Confirmed.** After the restart at 15:41:37 CEST, `SPY_short` opened
`SPY260901C00766000/SPY260901C00771000` at credit 1.82, and `NVDA` and
`DIA` appeared in the position list for the first time. All six 422
rejections disappeared.

### `ad109a5` — wing selection diverged from the simulator

**Symptom** (09:45 ET). `TLT_long` reached **20/20 and was halted**, having
never opened a position:

```
no wing strike below 82.5 (widths [5, 6, 4, 7, 3, 8, 10]) at 2026-09-02
```

**Cause.** The TLT chain that day, measured against the broker: whole
dollars below 80, half dollars above (`70 … 79, 80, 80.5, 81, 81.5, 82,
82.5 …`). With the short strike at 82.5, every configured width points at
a half dollar **below** 80 — 79.5, 78.5, 77.5, 76.5, 75.5, 74.5, 72.5 —
and not one of them exists. Verified from two independent sources: the
broker's contract list for that expiry (46 strikes; 76, 77, 78, 79, 80 in
range, no 77.5) and a quote request for the constructed symbol
`TLT260902P00077500`, which returns nothing while both neighbours quote.

The real defect is not the width list. It is a **divergence between the
two halves of this project**: `backtest_options.pick_spread` takes the
nearest *existing* strike (`min(wing_side, key=|k - want|)`), while the
agent demanded an exact hit. On SPY's dense dollar grid the two rules agree
and the difference is invisible; on TLT it is the difference between
trading and not — which means the live bot was not running the strategy
that had been measured.

**Fix.** The agent now uses the simulator's rule. Every distinct candidate
is priced in the **same** quote request and the first with a positive
marketable credit wins; the simulator likewise requires a positive net
credit before accepting a pair. An empty wing side still fails loud —
snapping to the nearest must not paper over a chain with nothing on that
side.

**Differential test** on the real TLT chain:

| rule | result at short strike 82.5 |
|---|---|
| old (exact match) | **no wing at all** — reproduces the live failure |
| new (nearest existing) | 72, 74, 75, 76, 77, 78, 79 |

**Confirmed.** After the restart at 15:47:13 CEST the halted instrument
traded: `TLT_long: OPEN leg 1/20 cluster 1: sold 1x
TLT260902P00082500/TLT260902P00077000 credit 0.59` — short 82.5, wing
77.0, the nearest existing strike to the 5-dollar request.

Regression test added (`test_wing_snaps_to_the_nearest_existing_strike`)
which rebuilds that exact chain shape; the old rule finds no wing on it.
Suite: 27 agent tests, 21 core tests.

---

### `5cac761` — a rebuy stepped onto its own wing

**Symptom** (09:39-10:07 ET). `GOOGL_long` failed every poll and reached 15
of its 20 strikes without opening leg 2.

**Cause.** The rejected order, read out of the log:

```
GOOGL260904P00340000   sell   sell_to_open
GOOGL260904P00335000   buy    buy_to_open
```

and in the account at that moment:

```
GOOGL260904P00340000   qty +1     <- the wing of leg 1
GOOGL260904P00345000   qty -1     <- the short leg of leg 1
```

The rebuy wants to SELL the very contract leg 1 bought as its protective
wing. The broker answers `422, code 42210000, "position intent mismatch,
inferred: sell_to_close, specified: sell_to_open"` and it is right:
selling one of a contract we are long one of does not open a short leg, it
closes the wing. Forcing the intent through would net the wing away and
leave the grid holding something other than the spread its state
describes.

Structural, not a GOOGL accident: the Kangaroo steps every rebuy further
out of the money, so whenever that step equals the wing width the new
short strike lands exactly on the previous leg's wing.

**Fix.** `select_spread` receives the account snapshot the loader already
reads once per poll and skips a short strike held LONG or a wing held
SHORT. Without a snapshot the behaviour is unchanged — the guard never
moves a strike on its own.

**Confirmed.** After the restart at 16:11:04 CEST the instrument built
three legs with no failures: 345/340, 337.5/332.5, and onward.

### `2477d66` — three phantom clusters, and the level the fix belongs on

**Symptom.** Three times the agent came back believing it held a cluster
the account no longer had, then failed every poll towards its halt.

**Cause**, measured from the order history against the stop times in the
log banner:

| instrument | close FILLED (UTC) | process stopped | gap |
|---|---|---|---|
| GOOGL_short | 13:46:18 | 13:47:13 | 55 s |
| GOOGL_short | 14:10:31 | 14:11:04 | 33 s |
| IWM_short | 14:15:49 | 14:16:24 | 35 s |

Every one lands inside the 60 second fill window
(`fill_requote_samples 30 x poll_fill_seconds 2`). `close_cluster` never
returned from `wait_filled_or_cancel`, so it booked nothing, wrote no
state and logged no CLOSE line. `wait_filled_or_cancel` itself is not at
fault — it already accepts a late fill after cancelling
(agent.py:429-435). It simply never got to run.

**Two wrong turns before the right one**, both worth recording:

1. *Guarding the deploy.* `update_and_restart.ps1` learned to wait for
   zero open orders. But the script that performs a deploy is the one
   already on disk, so a fix to it protects only the deploy AFTER the one
   that ships it — the second phantom happened while the guard sat in the
   repo, unused.
2. *Narrowing the guard to one instrument.* For a state repair touching
   only `GOOGL_short` I filtered the open-order check to
   `kang_GOOGL_short_*`. But `Stop-ScheduledTask` ends the ONE process all
   25 instruments live in: the check was instrument-scoped, the effect
   process-wide. That produced the third phantom, on `IWM_short`.

**Fix, at the right level.** Guarding the stop cannot work at all: a
crash, a reboot or a dead VPS produces the same state with no script to
ask first. The broker is the authority, and the `client_order_id` already
carries what is needed to ask it —
`kang_<INSTRUMENT>_c<cluster>_l<leg>_x_<stamp>`. On startup, before
`reconcile` declares the state inconsistent, the agent looks for a FILLED
close of its own current cluster. BOTH signals are required: the order AND
no remaining position of that cluster. The order alone can mean a
partially closed cluster; a missing position alone can be broker lag or a
bad state file, and silently discarding a real cluster on that basis would
be far worse than halting.

**Confirmed.** The very first start on the new code recovered the open
case without a human touching anything:

```
broker reports 120 order(s) since 2026-08-31
IWM_short: RECOVERED cluster 7: the broker filled its close
           (1 order, latest kang_IWM_short_c7_l0_x_1788185747981)
           but this agent was stopped before it could book it
agent start: 25 of 25 instrument(s) live
```

The two GOOGL clusters were not recovered: their state files had been
moved aside by hand before this code existed. The money is in the account
either way — only those two clusters' internal bookkeeping is gone, which
is why the broker counted 26 closed clusters at 10:28 ET while the log
counted 24.

### Two sources, always

Today produced the reason to stop trusting the agent's own log as a
complete record. At 10:06 ET the log reported 15 closed clusters and
+267 USD; the broker's order history reported 16 and +436 USD for the same
period. The missing one was the first phantom — a close that filled and
was never written down.

Since then every P&L figure in this project is read from the **broker's
order history** first, with the log quoted only as a comparison. A log can
have holes; today it demonstrably did.

## The day after: what the report was telling us

The agent traded Monday without a further defect. The two found on
Tuesday morning were both in the machinery that REPORTS on it, and one of
them was on the public page for eighteen hours.

They were found because a window kept opening on the desktop every five
minutes. Nobody was watching the job; the job had been failing since
09:55 CEST.

### `8717fff` — the fee was a rate, and rates go stale

**Symptom.** Every publish since 09:55 CEST aborted on
`reconstruction does not close: ... residual -7.37 over 310 contracts`.
The live page froze at its 09:50 snapshot. Twenty consecutive failures,
no alarm.

**Cause.** Measured against the account rather than reasoned about. The
fills, realized, unrealized and fee model were all IDENTICAL to the last
run that closed; only `cash` had moved, by exactly 7.37, with no fill
between the two readings. A cash move without a fill is a non-trade
activity, and the broker had 299 of them:

| type | sub | n | sum |
|---|---|---|---|
| FEE | OCC | 294 | -9.14 |
| FEE | ORF | 1 | -4.65 |
| FEE | REG | 1 | -0.79 |
| FEE | TAF | 1 | -0.44 |
| FEE | CAT | 1 | -0.10 |
| JNLC | | 1 | +100,000.00 |

15.12 booked against 7.75 modelled — the difference is the residual, to
the cent. The model was `0.025 USD x contracts`, which is the OCC
clearing rate and was the entire fee on day one. The four regulatory
charges settled at 20:0x UTC, after Monday's close, at rates no
per-contract constant reaches. The OCC bookings themselves are -0.03 and
-0.05, not 0.025 x quantity.

**Fix.** `AlpacaCli.activities()` pages the account's non-trade bookings;
the report sums the `FEE` rows and builds the chart's fee line from their
`created_at` stamps. A booking it cannot classify aborts the run by name
rather than being folded into a figure a reader takes for trading
performance. Cash paid in before the first fill is starting capital; cash
moved after it appears in the identity.

**Confirmed.** `equity 97,854.88 ... fees 15.12 (298 bookings) residual
+0.0002`.

### `8717fff` — and the curve was not this account

**Symptom.** None. That is the point: the page looked fine and every
number on it that a reader would check was right.

**Cause.** `--period 1D --timeframe 5Min` returned 200,000 for the open
of a day the account started at 100,000, and 199,130.16 for its close
against the broker's own `last_equity` of 99,105.88. The published curve
therefore ran flat at ~199,000 all Monday and fell off a cliff at the
live point, and the page reported a **maximum drawdown of -102,137.75**
that never happened.

Measured one parameter at a time:

| query | result |
|---|---|
| `--cashflow-types NONE` | identical output — excluded |
| `--pnl-reset no_reset` | identical output — excluded |
| `1D/1H` | +100,000.00 at every shared timestamp |
| `2D/5Min` | first point correct, later ones inflated |
| `5D/5Min`, `1W/5Min` | agree with `5D/1H` and with `account get` |
| `1M/1D` | 99,105.88 — `last_equity` to the cent |

Periods of 5D and longer read the account correctly; 1D and 2D do not.

**Fix.** `fetch_curve` takes 5D/5Min and refuses to draw it until it
agrees with a second series at every shared timestamp and with
`last_equity` at the previous close. Points from before the account
existed are dropped — the broker reports those as equity 0, and a zero in
the curve is a 100 % drawdown in the drawdown line, the same defect
wearing a different hat. The tolerance is 1 % of starting capital: the
measured spread between two resolutions of the same true series is at
most 111 USD on 99,000, and the defect it has to catch is 100,000.

**Confirmed.** Curve starts at 100,000 at Monday 09:30 ET; maximum
drawdown -2,145.12.

### Why neither said anything

The job runs every five minutes in a window nobody reads, and its log was
UTF-16 while every search over it was ASCII — the same encoding trap as
`3e00877`, in a second file. Under Windows PowerShell 5.1 with
`ErrorActionPreference = Stop`, a bare native call dies on the first line
its program writes to stderr, and the host renders that record AFTER the
redirect has closed. For a scheduled task, that is nowhere. Measured:

| pattern | what reaches the log |
|---|---|
| bare `& python ...` | the step banner, nothing else |
| `2>&1`, SilentlyContinue | **count 0** — even when the command SUCCEEDS |
| `2>$file`, SilentlyContinue | empty file |
| try/catch under Stop | the first line only |
| `2>&1`, Continue | the complete traceback, exit code intact |

Both native programs go through one helper on `Continue` now, and it
returns nothing unless asked: the `NativeCommandError` block that showed
up in the log on the first run after that change was an ErrorRecord
inside an *unconsumed return value*, not the preference talking. The task
hides its window, writes UTF-8, and puts the reason for a failure into
the log it failed in.

**A rate that is right today is a wrong number tomorrow, and a series
that is wrong looks exactly like a series that is right.** Both fixes are
the same move: stop deriving a number, go and read it, and check it
against a second source before showing it to anyone.

## What these findings have in common

The five in the agent were invisible to the backtest, and for the same
reason:
the simulator asks the broker *what exists* and prices it, while the live
agent additionally has to hold an account, *submit* orders, *re-submit*
them, and survive being stopped between a fill and its booking. The
research path and the execution path shared a strategy but not a code
path, and all five defects lived in that gap.

Two of them were a genuine DIVERGENCE — the live agent was not running the
strategy that had been measured — and both are now closed by making the
agent use the simulator's own rule:

- the wing had to match a width exactly, where the simulator snaps to the
  nearest existing strike;
- the rebuy could step onto a strike the account already held, which a
  backtest can never notice because it has no account to net against.

The rest have no counterpart in a simulator at all. A backtest has no
order ids, no fill windows, and no process that can be killed between a
fill and the booking of it. Those could only ever be found live — which is
the honest answer to why a bot with 32 agent tests and 21 core tests still
had five defects on its first trading morning, and why the tests written
*after* each one are the part of this log that matters most.

The two found on Tuesday are a different animal, and the more
uncomfortable one. They were not in the agent at all — the agent traded
correctly through both — they were in what the project SAID about itself.
A stale fee rate stopped the report loudly; a wrong equity series did not
stop it at all, and put a drawdown of -102,137.75 on a public page for
eighteen hours. Nothing in the output distinguishes a curve that is right
from a curve that is wrong, which is why that one is now checked against
a second reading of the same account before it is drawn.

Neither would have been found by looking harder at the code. The fee
defect was found by asking the broker what it had actually booked; the
curve defect by asking the same account the same question two different
ways and noticing the answers differed by 100,000. Both were found at all
only because a console window kept opening on the desktop every five
minutes.

Every fix here carries a regression test that reproduces the failure from
the real data behind it: the TLT chain with its mixed dollar and
half-dollar grid, the GOOGL leg-on-its-own-wing collision, a filled close
with no remaining position, the six fee bookings at six different amounts,
and the inflated 1D series set against the honest one. The suite could
not have found them first; it can keep them from coming back.
