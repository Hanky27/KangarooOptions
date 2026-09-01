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

"""The gate's safety property, tested against a model that misbehaves.

A prompt is a request, not a guarantee. Everything here assumes the model
answers badly - too large, negative, prose instead of JSON, nothing at all
- and checks that the account is never worse off than if the gate had not
been consulted.
"""

import json

import pytest

from risk_gate import RiskGate

VIEW = {"equity": 100000.0, "headroom_pct": 55.0, "clusters_open": 12,
        "leg_index": 6, "this_cluster": {"instrument": "SPY_long",
                                         "leg_index": 6}}


def gate(answer=None, raises=None, **cfg):
    """A gate whose only contact with the outside world is stubbed out."""
    params = {"enabled": True, "provider": "anthropic", "model": "m",
              "key_env": "TEST_GATE_KEY", "consult_from_leg": 4}
    params.update(cfg)
    g = RiskGate(params, say=lambda *_a, **_k: None)

    def _ask(_key, _requested, _view):
        if raises is not None:
            raise raises
        return answer
    g._ask = _ask
    return g


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("TEST_GATE_KEY", "test-key-not-real")


# --- the property that matters -----------------------------------------

@pytest.mark.parametrize("answered, expected", [
    (0, 0),          # veto
    (1, 1),          # reduced
    (3, 3),          # agreed
    (4, 3),          # larger than asked -> clamped
    (99, 3),         # far larger -> clamped
    (-2, 0),         # negative -> clamped
])
def test_the_gate_can_never_return_more_than_was_asked(answered, expected):
    """The clamp is the safety property, and it lives in code rather than
    in the prompt. A model cannot open a position, enlarge one, or choose
    a strike through this path no matter what it answers."""
    g = gate(json.dumps({"qty": answered, "reason": "because"}))
    d = g.review(3, VIEW)
    assert d.allowed == expected, (answered, d.allowed, d.reason)
    assert 0 <= d.allowed <= 3


def test_a_clamp_is_recorded_as_a_contract_violation_not_a_judgement():
    """A model answering outside its own contract is a defect to see in
    the counters, not a decision to quietly honour."""
    g = gate(json.dumps({"qty": 50, "reason": "load up"}))
    d = g.review(2, VIEW)
    assert d.allowed == 2
    assert d.source == "clamped"
    assert g.counters()["clamped"] == 1
    assert "50" in d.reason and "clamped" in d.reason


# --- every failure mode leaves the grid exactly as it was ---------------

@pytest.mark.parametrize("answer", [
    "I think you should probably reduce this a bit.",   # prose
    "",                                                  # empty
    "{}",                                                # no qty
    '{"qty": "two"}',                                    # wrong type
    '{"qty": null}',                                     # null
])
def test_an_unusable_answer_leaves_the_request_untouched(answer):
    g = gate(answer)
    d = g.review(7, VIEW)
    assert d.allowed == 7, d.reason
    assert d.source == "error"
    assert g.counters()["errors"] == 1


def test_an_unreachable_model_leaves_the_request_untouched():
    """Failing CLOSED would be a silent strategy change: an outage would
    turn a running grid into a different, untested one while it holds live
    positions."""
    g = gate(raises=TimeoutError("no route to host"))
    d = g.review(5, VIEW)
    assert d.allowed == 5
    assert d.source == "error"
    assert "TimeoutError" in d.reason
    assert g.counters()["errors"] == 1


def test_a_missing_key_is_an_error_and_not_a_veto(monkeypatch):
    monkeypatch.delenv("TEST_GATE_KEY", raising=False)
    g = gate(json.dumps({"qty": 0, "reason": "never reached"}))
    d = g.review(4, VIEW)
    assert d.allowed == 4
    assert d.source == "error"
    assert "TEST_GATE_KEY" in d.reason


# --- when the model is asked at all -------------------------------------

def test_a_shallow_cluster_with_room_never_reaches_the_model():
    """Most rebuys are routine; the token bill has to be proportional to
    the decisions that actually matter."""
    asked = []
    g = gate(json.dumps({"qty": 0, "reason": "veto"}))
    original = g._ask
    g._ask = lambda *a: (asked.append(a), original(*a))[1]
    view = dict(VIEW, leg_index=1, headroom_pct=90.0)
    d = g.review(1, view)
    assert d.source == "passthrough"
    assert d.allowed == 1
    assert asked == []
    assert g.counters()["consulted"] == 0


def test_thin_headroom_reaches_the_model_even_on_the_first_leg():
    g = gate(json.dumps({"qty": 0, "reason": "no margin left"}),
             consult_below_headroom_pct=40.0)
    view = dict(VIEW, leg_index=0, headroom_pct=12.0)
    d = g.review(1, view)
    assert d.source == "model"
    assert d.allowed == 0
    assert g.counters()["vetoed"] == 1


def test_a_disabled_gate_is_a_pure_passthrough():
    g = gate(json.dumps({"qty": 0, "reason": "veto"}), enabled=False)
    d = g.review(9, VIEW)
    assert d.allowed == 9
    assert d.source == "passthrough"
    assert g.counters() == {"enabled": False, "provider": None, "model": None,
                            "consulted": 0, "reduced": 0, "vetoed": 0,
                            "errors": 0, "clamped": 0}


# --- what survives into the audit trail ---------------------------------

def test_every_model_contact_is_recorded_and_the_list_stays_bounded():
    """The decisions ride along in the state file, which is written every
    poll - an unbounded list would grow without limit over a week."""
    g = gate(json.dumps({"qty": 1, "reason": "trimmed"}))
    for _ in range(60):
        g.review(2, VIEW)
    assert len(g.decisions) == 50
    assert g.counters()["reduced"] == 60
    assert all(d["allowed"] <= d["requested"] for d in g.decisions)


def test_a_rambling_reason_is_cut_to_its_first_words():
    g = gate(json.dumps({"qty": 1, "reason": " ".join(["word"] * 60)}),
             max_reason_words=20)
    d = g.review(2, VIEW)
    assert d.reason.endswith("...")
    assert len(d.reason.split()) == 21


def test_an_unknown_provider_is_refused_at_construction():
    """A typo in the config must not silently become a gate that never
    reaches anything and reports 'error' forever."""
    with pytest.raises(ValueError) as err:
        RiskGate({"enabled": True, "provider": "gpt-5-turbo-max"})
    assert "gpt-5-turbo-max" in str(err.value)


def test_the_request_body_carries_the_view_and_the_system_contract():
    """Both providers get the same instruction; only the envelope differs."""
    for provider, probe in (("anthropic", lambda b: b["system"]),
                            ("featherless",
                             lambda b: b["messages"][0]["content"])):
        g = gate("{}", provider=provider,
                 model=None)
        prompt, body = g._payload(3, VIEW)
        assert "Requested quantity: 3" in prompt
        assert "headroom_pct" in prompt
        assert "never exceed the requested quantity" in probe(body)
        assert body["model"], f"{provider} has no default model"
