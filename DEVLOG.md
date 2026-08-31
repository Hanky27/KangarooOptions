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

## What these two have in common

Both were invisible to the backtest, and for the same reason: the
simulator asks the broker *what exists* and prices it, while the live
agent additionally has to *submit orders* and *re-submit* them. The
research path and the execution path shared a strategy but not a code
path, and both defects lived in the gap.

The wing fix closed part of that gap by making the agent use the
simulator's own rule. The order-id fix has no counterpart in the simulator
at all — there are no order ids in a backtest, which is exactly why it
could only ever be found live.
