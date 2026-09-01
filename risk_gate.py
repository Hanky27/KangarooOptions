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

"""An LLM risk gate that can only take risk away.

WHAT IT DECIDES, AND WHY THAT AND NOT SOMETHING ELSE
The grid itself needs no opinion about direction: it sells a credit
spread, and when the underlying moves against it, it sells another one
further out with 1.1x the size. What kills a martingale grid is never one
bad instrument - it is MANY instruments deep at the same time, each one
correctly following its own rule, together consuming the margin that any
one of them would have needed to recover.

That is a portfolio judgement, and it is the one thing a single
instrument's state machine structurally cannot make: KangarooCore sees
its own legs and nothing else. So this is where a model earns its place -
it is handed the whole account at once and asked which grids may keep
adding and which should hold.

THE INVARIANT, ENFORCED IN CODE AND NOT BY THE PROMPT
The gate may return a quantity between zero and the one the grid asked
for. Nothing else. A model that answers 50 when the grid asked for 2 gets
clamped to 2 and the violation is logged and counted; a model that
answers anything unparseable leaves the request untouched. There is no
path through this file by which a model can open a position, enlarge one,
choose a strike, or change a stop. The worst a compromised or hallucinating
model can do is stop the grid from adding - which is the same thing
`max_adverse_pct` already does with a constant.

WHEN THE MODEL IS NOT ASKED
Most rebuys are not interesting. The gate consults the model only when a
cluster is at or past `consult_from_leg` legs, or when account margin
headroom has fallen below `consult_below_headroom_pct`. Everything else
passes through untouched and costs nothing. This keeps the token bill
proportional to the decisions that actually matter and keeps the poll
loop fast.

WHEN IT FAILS
Loudly, and open. An unreachable endpoint, a timeout, a refusal, a
malformed answer: each is logged with its reason, counted, and the grid
proceeds with the quantity it asked for. Failing CLOSED would be a silent
strategy change - a model outage would quietly turn the grid into a
different, untested one while it holds live positions. The counters are
published on the performance sheet so that "the gate was down" can never
look like "the gate agreed".
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# The two providers this was built against. Both speak the same shape of
# request; only the URL, the auth header and the envelope differ.
PROVIDERS = {
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "auth": lambda key: {"x-api-key": key,
                             "anthropic-version": "2023-06-01"},
        "default_model": "claude-opus-5",
    },
    "featherless": {
        "url": "https://api.featherless.ai/v1/chat/completions",
        "auth": lambda key: {"Authorization": f"Bearer {key}"},
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
    },
}

SYSTEM = """You are the risk gate of an options credit-spread grid that is \
already running. You do not choose instruments, strikes, expiries or \
direction, and you never open anything: the grid has decided to add a leg \
and you decide only how much of it, if any, survives.

The grid sells credit spreads. When the underlying moves against a \
cluster it adds another spread further out of the money, 1.1x larger each \
time. Each cluster is right to do this on its own. The danger is many \
clusters deep at once: together they consume the margin any single one \
would need to recover, and a forced reduction at the wrong moment turns a \
recoverable grid into a realised loss.

You see the whole account. Decide whether THIS cluster may add the size it \
asked for, a smaller size, or none at all.

Answer with one JSON object and nothing else:
{"qty": <integer 0..requested>, "reason": "<at most 20 words>"}

qty must never exceed the requested quantity. Prefer the full quantity \
unless the account view gives you a concrete reason to reduce it: thin \
margin headroom, many clusters simultaneously deep, or this cluster \
already carrying an outsized share of the open risk. Say which one."""


class GateDecision:
    """What the gate decided, and everything needed to audit it later."""

    def __init__(self, requested: int, allowed: int, reason: str,
                 source: str, latency_ms: int = 0,
                 raw: str | None = None) -> None:
        self.requested = requested
        self.allowed = allowed
        self.reason = reason
        self.source = source          # model | passthrough | error | clamped
        self.latency_ms = latency_ms
        self.raw = raw

    @property
    def changed(self) -> bool:
        return self.allowed != self.requested

    def as_dict(self) -> dict:
        return {"requested": self.requested, "allowed": self.allowed,
                "reason": self.reason, "source": self.source,
                "latency_ms": self.latency_ms}


class RiskGate:
    """Consults a model about size, and can only ever reduce it."""

    def __init__(self, cfg: dict, say=print) -> None:
        self.enabled = bool(cfg.get("enabled", False))
        self.provider = cfg.get("provider", "anthropic")
        if self.enabled and self.provider not in PROVIDERS:
            raise ValueError(
                f"risk_gate.provider {self.provider!r} is not one of "
                f"{sorted(PROVIDERS)}")
        spec = PROVIDERS.get(self.provider, {})
        self.model = cfg.get("model") or spec.get("default_model")
        self.key_env = cfg.get("key_env", "LLM_API_KEY")
        self.timeout = float(cfg.get("timeout_seconds", 20))
        self.consult_from_leg = int(cfg.get("consult_from_leg", 4))
        self.headroom_pct = float(cfg.get("consult_below_headroom_pct", 40.0))
        self.max_words = int(cfg.get("max_reason_words", 20))
        self.say = say

        # Counters, published so an outage can never read as agreement.
        self.consulted = 0
        self.reduced = 0
        self.vetoed = 0
        self.errors = 0
        self.clamped = 0
        self.last: GateDecision | None = None
        self.decisions: list[dict] = []

    # --- the only entry point --------------------------------------------

    def review(self, requested: int, view: dict) -> GateDecision:
        """Return a decision whose `allowed` is in 0..requested.

        `view` is the account and cluster state the model is shown; it is
        built by the caller so that this file never reaches for data of
        its own.
        """
        if requested <= 0:
            return self._record(GateDecision(requested, requested,
                                             "nothing requested",
                                             "passthrough"))
        if not self.enabled:
            return self._record(GateDecision(requested, requested,
                                             "gate disabled", "passthrough"))
        if not self._worth_asking(view):
            return self._record(GateDecision(
                requested, requested,
                f"leg {view.get('leg_index')} below consult threshold "
                f"{self.consult_from_leg}", "passthrough"))

        key = os.environ.get(self.key_env, "")
        if not key:
            self.errors += 1
            return self._record(GateDecision(
                requested, requested,
                f"no key in {self.key_env} - grid proceeds unchanged",
                "error"))

        started = time.time()
        try:
            raw = self._ask(key, requested, view)
        except Exception as exc:                              # noqa: BLE001
            self.errors += 1
            return self._record(GateDecision(
                requested, requested,
                f"{type(exc).__name__}: {str(exc)[:120]}", "error",
                int((time.time() - started) * 1000)))
        latency = int((time.time() - started) * 1000)
        self.consulted += 1
        return self._record(self._interpret(raw, requested, latency))

    # --- pieces -----------------------------------------------------------

    def _worth_asking(self, view: dict) -> bool:
        """Most rebuys are routine. Ask only about the ones that are not."""
        if int(view.get("leg_index", 0)) >= self.consult_from_leg:
            return True
        headroom = view.get("headroom_pct")
        return headroom is not None and float(headroom) < self.headroom_pct

    def _payload(self, requested: int, view: dict) -> tuple[str, dict]:
        prompt = (f"Requested quantity: {requested}\n\n"
                  f"Account and cluster state:\n"
                  f"{json.dumps(view, indent=1, sort_keys=True)}")
        if self.provider == "anthropic":
            return prompt, {
                "model": self.model,
                "max_tokens": 200,
                "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            }
        return prompt, {
            "model": self.model,
            "max_tokens": 200,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
        }

    def _ask(self, key: str, requested: int, view: dict) -> str:
        spec = PROVIDERS[self.provider]
        _prompt, body = self._payload(requested, view)
        headers = {"content-type": "application/json"}
        headers.update(spec["auth"](key))
        req = urllib.request.Request(
            spec["url"], data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
        if self.provider == "anthropic":
            return "".join(b.get("text", "")
                           for b in answer.get("content", []))
        return answer["choices"][0]["message"]["content"]

    def _interpret(self, raw: str, requested: int,
                   latency: int) -> GateDecision:
        """Parse, then CLAMP. The clamp is the actual safety property."""
        text = (raw or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            self.errors += 1
            return GateDecision(requested, requested,
                                f"no JSON object in answer: {text[:80]!r}",
                                "error", latency, raw)
        try:
            parsed = json.loads(text[start:end + 1])
            qty = int(parsed["qty"])
        except (ValueError, KeyError, TypeError) as exc:
            self.errors += 1
            return GateDecision(requested, requested,
                                f"unusable answer ({exc}): {text[:80]!r}",
                                "error", latency, raw)

        reason = " ".join(str(parsed.get("reason", "")).split())
        words = reason.split()
        if len(words) > self.max_words:
            reason = " ".join(words[:self.max_words]) + " ..."

        source = "model"
        if qty > requested or qty < 0:
            # Not a judgement call: a model outside its own contract.
            self.clamped += 1
            source = "clamped"
            reason = (f"model answered {qty} against a request of "
                      f"{requested} - clamped. {reason}")
            qty = max(0, min(qty, requested))
        if qty == 0:
            self.vetoed += 1
        elif qty < requested:
            self.reduced += 1
        return GateDecision(requested, qty, reason or "no reason given",
                            source, latency, raw)

    def _record(self, decision: GateDecision) -> GateDecision:
        self.last = decision
        if decision.source in ("model", "clamped", "error"):
            self.decisions.append(decision.as_dict())
            # Bounded: this list is written into the state file every poll.
            del self.decisions[:-50]
        return decision

    def counters(self) -> dict:
        return {"enabled": self.enabled,
                "provider": self.provider if self.enabled else None,
                "model": self.model if self.enabled else None,
                "consulted": self.consulted, "reduced": self.reduced,
                "vetoed": self.vetoed, "errors": self.errors,
                "clamped": self.clamped}
