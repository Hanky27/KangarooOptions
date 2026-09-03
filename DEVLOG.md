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
| `5D/5Min`, `1W/5Min` | agreed with `5D/1H` and with `account get` (and stopped agreeing six hours later) |
| `1M/1D` | 99,105.88 — `last_equity` to the cent |

Periods of 5D and longer read the account correctly; 1D and 2D do not.

**First fix, and why it was not enough.** `fetch_curve` took 5D/5Min —
the series that agreed with the account that morning — and refused to draw
it unless it matched a second series and `last_equity`. Curve confirmed
starting at 100,000, maximum drawdown -2,145.12.

**Six hours later the same check fired, in the opposite direction.** With
the market open, `5D/1H` had become the inflated one and `1D/5Min` the
clean one:

| query | base_value_asof | account existed then | result |
|---|---|---|---|
| Monday window | 2026-08-28 | no | +100,000 |
| Tuesday window | 2026-08-31 | yes | clean |
| `1D` pre-market | 2026-08-28 | no | +100,000 |
| `1D` market open | 2026-08-31 | yes | clean |
| `5D/1H` | 2026-08-27 | no | +100,000 |
| `1M/1D` | 2026-08-27 | no | **clean** |

An INTRADAY series whose baseline predates the funding comes back inflated
by it; the daily series does not. **No fixed (period, timeframe) is safe**,
which is the real lesson: a rule picked because it matched this morning is
a coin that has landed heads once.

**Second fix, and the one that stands.** The daily series is the SPINE, and
it is verified against `last_equity` before anything is built on it. Each
day's intraday window is then admitted only if it lands on that day's spine
value; Monday — the funding day — does not, and contributes its single
close instead of a line 100,000 too high. Today has no close yet, so it
gets two checks instead: its baseline must equal yesterday's verified
close, and its newest bar must be within 1 % of the account as it stands.
Every refusal is published under `curve_rejected_days` with the numbers
that caused it, so a reader sees the check happen rather than being told
it did.

**Today's bars failed that check too.** Measured 12:24 ET:

```
account.equity                    90,321.33
cash + sum(market_value)          90,455.33   <- agrees within 134.00
newest 5Min history bar (12:20)   95,595.88   <- 5,274.55 above both
the 12:15 bar, read twice         95,911.88, then 94,950.88
```

The intraday history is not the same quantity as the account's own equity
while the day is open, and the broker revises it as it goes. So today
contributes one point — the live one, from cash and the position marks. A
straight line from yesterday's verified close says less than a shape would,
and every level on it is one the account actually held.

**`start_equity` stopped coming from the endpoint** at the same time. It is
now the sum of the account's own cash transfers booked before the first
fill: 100,000.00, which is also what the competition requires the account
to start at. `base_value` was never that number — it was whatever baseline
the query happened to pick, and it was measured naming a date on which this
account did not exist.

**A third term appeared while fixing the second.** With the market open the
identity was short by 4.55 on 492 contracts. Every one of the account's 299
non-trade bookings carried date 2026-08-31; today had none. Contracts
traded since the newest booked fee: 182. 182 x 0.025 = 4.55, exactly. Cash
is charged a provisional 0.025 per contract the instant a fill happens and
the settled bookings replace it after the close — which for Monday came to
15.12, not 7.75. The report now carries both terms: `fees` is what the
broker booked, `fees_provisional` is what cash has been charged beyond it,
and the second is bounded by the provisional rate on the contracts traded
since the newest booking. Outside that bound the run still aborts.

### `8a84e63` — the batch had no size, and the grid outgrew it

**Symptom.** The agent printed its startup banner, died before the first
poll, was restarted by the watchdog two minutes later, and died the same
way. From **18:03 to 19:36 CEST** — 93 minutes, with the market open, 25
grids holding open positions, nothing closing and nothing managed. The log
held 46 identical startup banners and not one error line.

**Cause**, in the broker's own words, from the request that killed it:

```
{"error": "symbol limit is 100", "status": 400,
 "path": ".../v1beta1/options/quotes/latest?symbols=AAPL260902C00312500%2C..."}
```

The account crossed **100 open option contracts** at 18:03. Every poll
after that asked for 102 symbols in one request, and the endpoint refused
ALL of it — no partial result — so `market_data()` raised before the first
instrument was stepped.

Batching option quotes is what makes the poll cost near-constant in the
instrument count instead of linear, and it stays. What was missing is that
a batch has a size, and that the size is a property of the endpoint rather
than of the grid.

**Fix.** `QUOTE_SYMBOL_LIMIT = 100`, and both quote calls split their
symbol list into request-sized pieces and merge the answers. The constant
is 100 because the endpoint says so in the error text, not because it
seemed round.

**Confirmed against the live account**, not only a stub: 102 open legs in,
102 quotes back, none missing. Two regression tests cover the split at 102
and the boundary at exactly 100.

**What this one is really about.** Every other defect in this log was
found by looking at a number that was wrong. This one was found by
noticing that a machine which had been asked to run for a week had stopped
answering — and it had been silent for an hour and a half before anybody
asked. The agent's own log could not say so, because the failure happened
before the part of the run that writes anything.

### `4426150`, `8d139b6` — the model, live

The gate went on at 19:42 CEST on `claude-sonnet-5`, after the three
candidates were measured against the real account with the real view:

| model | latency | answer to a request of 3 |
|---|---|---|
| `claude-opus-5` | 6.56 s | 1 |
| `claude-sonnet-5` | 4.03 s | 1 |
| `claude-haiku-4-5` | 1.42 s | 0 |

The first attempt at that measurement failed on my own defect rather than
theirs: `max_tokens` was 200, two of three answers came back cut — one
mid-JSON (`{"qty": 1, "reason`), one empty — and both were logged as
unusable. It is 1024 now.

Its first decision on the competition account:

```
GOOGL_long: RISK GATE [model] 2 -> 1 (10124 ms): Multiple clusters
(AMZN, TSLA, GLD, SLV) already breached 5% adverse simultaneously;
reduce to conserve shared margin.
```

It halved the size and justified it by naming four OTHER instruments —
which is the entire argument for a model in this position rather than a
threshold. `KangarooCore` sees its own legs and nothing else.

Note the 10.1 seconds against the 4.03 measured earlier: the live view
carries all 25 clusters and the model reasoned longer over it. That is why
a poll spends at most three consults — 3 x 10 s is a bound the loop can
carry, 25 x 10 s is not.

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

### `46c2d7d` — the curve, third attempt, and what a chart may claim

**Symptom.** The published chart was four points and a cliff. The previous
fix had refused today's intraday bars, correctly, and refusing them left
nothing to draw.

**Is the broker's series merely LAGGING?** It was worth asking: one bar had
been seen revised downward by 961, toward the account. Read the same
series twice, three minutes apart:

| bar | read 1 | read 2 | moved |
|---|---|---|---|
| 13:00 ET | 95,671.88 | 95,587.88 | −84.00 |
| 13:05 ET | 95,609.88 | 95,525.88 | −84.00 |
| … eleven bars … | | | −84.00 |
| 13:55 ET | 94,641.88 | 94,566.88 | −75.00 |
| 14:05 ET | 95,209.88 | 95,079.88 | −130.00 |

Eleven bars moved by *exactly the same amount at the same time*, and bars
older than 65 minutes did not move at all. That is a block shift applied
to a recent window, not marks settling one by one — and the newest bar
still sat **4,016.40** above the account. The series does not converge to
`account.equity` because it is not measuring it.

**Could the past be re-marked instead?** Equity is not a number that has
to be fetched:

    equity(t) = cash(t) + sum over held contracts of qty(t) * mark(t) * 100

`cash(t)` and `qty(t)` follow from the fills, which are timestamped to the
millisecond. Only `mark(t)` has to come from outside.
`tools_equity_backfill.py` does all of it and checks itself against the
live measurements before writing a single point. It gets:

```
{"error": "OPRA agreement is not signed", "status": 403,
 "path": ".../v1beta1/options/bars?..."}
```

Latest quotes work — that is the feed the agent trades on — but historical
option BARS need an entitlement this account does not have. The past
cannot be re-marked, by that tool or any other. The file stays: the dead
end is worth recording and the reconstruction is correct the moment the
entitlement exists.

**Fix.** Draw what can be proved. The **balance** line moves only when a
fill books something and every fill is timestamped, so it is exact for
every minute of the week without asking anyone — 470 points and dense. The
**equity** line keeps only the points this report measured itself and
reconciled to the cent, and gains one every five minutes. A point with no
value for a series is a GAP in the path, not a NaN: `pathOf` starts a new
subpath after every hole, the y-range skips nulls, and the band between
the lines — which is the open P&L — is drawn only where equity is known.

**What this cost to learn.** Three attempts, and the first two were
published. A chart is the one artefact where being wrong is invisible.

## Why the backtests looked so much better

The account sat at −9 % on day two while the strategy it runs was
published as profitable. That gap is not a mood, it is a measurement, and
this is it.

### The live configuration is not the backtested one

`configs/meta_short.yaml`, verbatim from its own header:

```
# Window 2026-08-14..2026-08-28, hourly RTH bars, real option chain.
#   wins/week 7.73  trades/week 7.73  win rate 1.0
#   net +1771 USD realized
#   worst cluster +15   equity dd -2665
# Ten trading days is a short window - every figure above carries
# that caveat.
```

**A win rate of 1.0 over ten trading days**, worst cluster +15. All 25
configs come from that search (`tools_week.py`), not from the 2.5-year
sweep the README quotes.

### The backtests never paid the bid/ask

`backtest_options.py` prices every option at the bar CLOSE — a traded
print. `--cost_usd` exists and every published run set it to **0**. The
README already said so in one clause: *"marks are trade closes without
spread costs"*.

Measured on the live book, 188 open contract-legs:

| | |
|---|---|
| half the bid/ask, whole book, one crossing | **3,284.90 USD** |
| per contract-leg | 17.47 USD (median 8.50) |
| per SPREAD, one crossing | **34.95 USD** |
| round trip per spread | 69.89 USD |
| median quote width | **4.9 % of mid** (worst quartile 7.8 %) |

The same run, SPY put-only, 2024-02..2026-08, changing nothing but that
number:

| cost per spread per execution | realized | max drawdown |
|---|---|---|
| **0** — every published run | **+2,850.00** | −2,994 |
| 17.00 — median-based | **−433.00** | −3,716 |
| 34.95 — measured | **−6,172.65** | −7,024 |

And with both directions running, as live: **−14,033.65**.

**The entire published profit is smaller than the cost that was never
charged.**

### The faster it decides, the better it looks — and the worse it does

Same window, same parameters, only the decision rate:

| resolution | cost | realized | legs per cluster |
|---|---|---|---|
| daily | 0 | −186 | 1.32 |
| daily | 34.95 | −2,379 | 1.29 |
| **hourly** | **0** | **+4,781** | 1.83 |
| hourly | 34.95 | −487 | 2.10 |

Higher cadence produces more legs, every leg is another crossing, and a
zero-cost backtest counts the extra legs as free upside. The live agent
polls every **five seconds** and runs about 3.5 legs per cluster — more
than any resolution ever tested.

That is why the hourly week-search produced a win rate of 1.0.

### A proposal of mine, tested and refuted

The obvious response to a deep grid is to make the gate brake earlier —
`consult_from_leg` from 4 to 2. Its deterministic equivalent is a cap on
legs, and the cap was swept (SPY hourly, cost 34.95):

| leg cap | realized | max drawdown |
|---|---|---|
| 20 | −486.90 | −2,967 |
| **6** | **−482.35** | **−2,247** |
| 4 | −2,278.60 | −3,007 |
| 3 | −2,153.35 | −2,656 |
| 2 | −3,042.35 | −3,414 |
| 1 | −3,456.25 | −3,895 |

**Braking earlier is monotonically worse.** The martingale recovers
*through* the legs it adds; cutting them realises the loss the next leg
would have earned back — which is what `max_adverse_pct`'s own design note
says, and which I proposed to override anyway. The one thing worth
keeping: a cap of **6** holds the result and takes 24 % off the drawdown.

### A lead that did not replicate, recorded because it did not

Raising the take-profit looked like the answer on SPY: 0.02 → −2,393.75,
0.10 → −486.90, 0.20 → +689.05, 0.40 → +1,971.55. Monotone, sign-flipping,
with a smaller drawdown. Two more symbols were run before it could be
called a finding:

| tp_pct | SPY | META | QQQ |
|---|---|---|---|
| 0.02 | −2,393.75 | +489.90 | −2,203.55 |
| 0.10 | −486.90 | **+1,259.35** | −2,031.85 |
| 0.20 | +689.05 | +667.55 | −2,310.20 |
| 0.40 | +1,971.55 | +1,009.45 | −2,363.30 |

META peaks in the middle, QQQ is flat to worse, and both ran on 3–16
clusters. **It does not replicate.** The SPY column alone would have made
a convincing slide, which is exactly why it is written down as a
non-finding instead.

### What is not broken

44 clusters closed, **+2,135 realized**, a mean of +48.52 each. The books
reconcile to the cent. 25 of 25 instruments live, no halts. The whole loss
sits in open positions, and the market moved against them: 12 of 14
underlyings down since Monday's open, median −1.43 %.

The system does what it was built to do. What was wrong was the
expectation — built on runs that charged no transaction cost, tuned on ten
days with a 100 % win rate, at a decision rate slower than the real one.

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

---

## Thursday: eight defects, and how each one was hidden

The account's first assignment weekend arrived on Wednesday night, and by
Thursday morning nothing about the system was quite what it claimed. Five
defects in the agent, three in the reporting, and every single one had a
reason it had stayed invisible.

### 1. The fix for the last defect deleted a method

`32977da` — the pagination fix, and a good one — replaced the source slice
from `def orders_since(` to `def order_get(`. `activities()` lived between
them. The commit reads **39 insertions, 54 deletions** for a new function
of 37 lines. The balance was in the diff and nobody looked at it.

`tools_perf_snapshot.py:479` calls `cli.activities()`, so the publisher
raised `AttributeError` every five minutes for 36 hours, 498 runs, and the
public page froze at `2026-09-01T14:09:55-04:00` while the write-up
claimed it republished every five.

### 2. A live code path read a file that exists on one machine

`fetch_alpaca_bars.py` hardcodes

    ENV_FILE = "C:/Users/HMz/Documents/Source/McpServer/.../env.txt"

and `agent.settlement_close` called into it. On the trading host neither
that path nor the CLI beside it exists, so every settlement of an expired
leg raised `FileNotFoundError`. IWM_short halted at startup; AAPL_short
after 20 consecutive failed polls.

The agent's own broker handle takes both paths from `config.yaml`, which
is why the same code runs on a workstation and on a VPS. Settlement now
goes through it.

### 3. An assignment could deadlock an instrument forever

`handle_expiries` defers settling an expired leg while the account still
holds the assigned stock — booking an intrinsic settlement beside the
shares would count the same money twice. `reconcile`, next in line, then
found the leg missing and halted the instrument. **And a halted instrument
never reaches `step()`, which is the only place `assignment_gate` flattens
the stock that caused the deferral.** GLD_long sat in that loop with 100
GLD in the account. No restart could ever have cleared it.

### 4. And it would have flattened the same stock twice, that afternoon

`assignment_gate` reads the position snapshot the loader takes **once per
poll** and shares with all 25 instruments. AMZN_long and AMZN_short both
see the same `-100 AMZN` row. Both buy 100. The account ends up +100 long
a stock nobody chose.

It had never fired — zero `assign_flat` orders in the account's entire
history, because no assignment had yet met an open market. Four
underlyings held assigned stock and the bell was four hours away.

### 5. The broker trades this account too, and it does not use our order ids

GLD_long was still halted after (3) was fixed, and the reason was not an
assignment. On 2026-09-02 at 19:45:07Z, fifteen minutes before the close,
three filled orders appeared that this agent never submitted:

    GLD260902P00401000  buy 1 @ 0.02   coid 6e8b6bab-7b07-...
    GLD260902P00405000  buy 1 @ 2.92   coid a8349f27-aa0c-...
    (mleg, symbol null)      @ 4.13    coid f4d852c1-c265-...

Broker UUIDs, not the `kang_<instrument>_c<cluster>_l<leg>_<kind>` scheme
every order of ours carries. Alpaca's paper engine closes short options by
itself near expiry. `recover_unbooked_close` could not see them twice
over: it matches on our prefix, and it only ends a cluster when *every*
leg is gone — this was three of four.

`book_broker_closes` books such a leg and books nothing it cannot
evidence: the short exit must be a filled buy at a price the broker
reports, the wing either a filled sell or an `OPEXP` row. A wing still
held raises rather than valuing half a spread.

The third of those three fills is the reason this was **dry-run against
the live state files before it was deployed**: the dry run printed three
broker fills where the code could see two. The mleg shape carries
`symbol: null` at the top level and the real symbols inside `legs`, so
reading only the top level would have missed a whole spread. The same dry
run confirmed what the fix was worth — **+49.00 on GLD_long, and nothing
else** — which is exactly what the deployed run then booked.

### Why 93 passing tests saw none of it

Every one of them replaced the boundary that was broken. The settlement
tests monkeypatched `agent.fetch_bars` itself; the reporting tests hand
the code a fake CLI, and a fake CLI grows whatever method it is asked for.

So `test_cli_contract.py` reads the **source** instead of calling it:
every `cli.<name>(` in the repo must exist on `AlpacaCli`, and nothing
reachable from `agent.py` may hardcode an absolute path. Both were
verified by putting each defect back and watching the new test fail.

## The reporting, and a premium counted twice

With `activities()` restored, the report immediately refused four booking
types it had never seen: `OPEXP`, `OPASN`, `OPEXC`, `OPTRD`. Measured over
all 32 rows: the first three move **exactly 0 USD** — they only take the
option out of the account — and `group_id` pairs each one with its share
trade (21 groups, every `OPASN`/`OPEXC` with an `OPTRD`, every `OPEXP`
alone).

They still have to reach the books, because there is no closing fill for
any of them. An option sold for a credit and then assigned leaves that
credit in cash with nothing on the other side of the identity.

**The first model was wrong, and the correction took two attempts.**
Closing the option at zero and buying the shares at the strike counts the
premium twice: the broker folds it into the share basis instead. Its own
`avg_entry_price` says so —

| | strike | broker basis | what happened |
|---|---|---|---|
| GLD | 405 | 402.330 | assigned |
| SLV | 59.875 | 59.125 | assigned |
| TLT | 82.5 | 81.900 | assigned |
| AMZN | 265 | **265.000** | exercised |

The identity came up **927.00** short, to the cent the sum of the three
assigned differences. Then I tried leaving the basis at the strike for
`OPEXC` and reported that it made things *worse* — 373 to 567 — and
therefore that the idea was wrong.

It was wrong because I had changed one of two coupled things. The premium
has to be somewhere, and there are exactly two places:

    short taken away (OPASN)   option closes at BOOK, realizing nothing
                               shares at strike ± premium
    long exercised   (OPEXC)   option closes at ZERO, realizing −premium
                               shares at the strike

Both halves move together or neither does. Checked the other way too: a
312.5/317.5 call spread that finished fully in the money realizes
`(credit − 5) × 100` under this rule, which is what a five-wide spread
losing every dollar of its width is worth.

### A bounded term was swallowing the rest

With the pairing fixed the identity closed — and **3.00 was sitting in the
provisional fee term**, which is bounded at 43.30 and would have hidden
anything under it. It was not a fee. It was SLV: the broker's basis folds
600.00 of premium into 800 assigned shares where the six orders that sold
those puts received **597.00**, verified against the fill activities and
the orders separately.

Two costs for one position is the disease. Unrealized is now measured
against the basis this report rebuilt from the cash that moved, not the
broker's `unrealized_pl` field, so both halves of the identity speak the
same language — and where the two views differ it is **published, per
symbol, on the page**. Residual 0.0000, provisional fees 0.00, 99 of 99
positions rebuilt.

### Two more reasons the page was down

**The history endpoint errors before the opening bell.** Asking for
today's 09:30–16:00 window when the session has not started is not an
empty answer: it clamps `end` to the last session it has and then rejects
its own window. Every publish between midnight and the open died on it.

**The live fetch had been reading a two-day-old snapshot.**
`cache: "no-store"` only bypasses the *browser's* cache.
`raw.githubusercontent.com` sits behind a CDN, and a CORS request gets a
different cached variant than a plain one — measured seconds apart, `curl`
received the snapshot stamped 07:09:51 ET while the page's own fetch
received one from 2026-09-01. The freshness check refused to go backwards
and kept the built-in copy, so nothing wrong was ever shown; the fetch had
simply stopped being live. A unique query string is a different cache key.

**And the one that could not be found by running the command.** The
scheduled task has an empty `WorkingDirectory`, so Windows starts it in
`C:\Windows\System32`. The default `--equity-log` is the relative
`state/equity_log.jsonl`, which there means creating a `state` folder
inside System32:

    PermissionError: [WinError 5] Zugriff verweigert: 'state'

every five minutes, while the identical command typed in the repo worked
every time. Verified the way the task does it, not the way I do it: run
from `C:\Windows\System32` it now completes with residual 0.0000, and the
task reports `rc=0` where it had reported `rc=1` for 36 hours.

## What Thursday has in common with Monday

Monday's lesson was that a number nobody checks is a number nobody can
trust. Thursday's is narrower and worse: **every one of these eight was
invisible in exactly the place someone would have looked.**

The deleted method was in the diff, as a line count. The workstation path
was in a module the tests replaced. The deadlock needed two correct
behaviours to meet. The double-flatten needed an assignment and an open
market on the same day, which had never happened. The broker's own orders
look like ours until you read the id. The premium was on both sides of an
identity that still balanced to within a bounded tolerance. And the
`System32` failure existed only in a context that is never typed by hand.

Nothing was found by reading code harder. Each one was found by asking the
account a question and comparing the answer to what the code assumed —
and, in the one case where I reasoned instead of measured, by an
experiment that told me my explanation was wrong.

107 tests. The page is live again.
