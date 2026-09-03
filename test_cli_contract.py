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

"""The two things the unit tests could not see, because they mocked them.

Both defects here cost live trading days and neither showed up in 93
passing tests, for the same reason: every test replaced the boundary that
was broken, so the tests proved the callers were consistent with a stub
instead of with the real thing.

DEFECT ONE - a method deleted by an anchor-to-anchor edit.
32977da replaced the source slice from `def orders_since(` to
`def order_get(`. `activities()` lived between them, so the rewrite
deleted it: 39 insertions against 54 deletions for a new function of 37
lines, and the balance was never looked at. tools_perf_snapshot.py calls
`cli.activities(...)`, so the publisher raised AttributeError every five
minutes for 36 hours and the public page froze at
2026-09-01T14:09:55-04:00. Nothing failed, because test_perf_snapshot.py
tests the pure functions and hands the reporting code a fake CLI - and a
fake CLI grows whatever method it is asked for.

DEFECT TWO - a live code path reading a file that only exists on one machine.
fetch_alpaca_bars.py carries module-level CLI_PATH and ENV_FILE pointing
at C:/Users/HMz/Documents/Source/..., and agent.settlement_close called
into it. On the trading host neither path exists, so every settlement of
an expired leg raised FileNotFoundError. Measured on VPS2 2026-09-03:
IWM_short halted at startup, AAPL_short after 20 consecutive failed
polls - 4 of 25 instruments dead. The four settlement tests passed
throughout, because they monkeypatched `agent.fetch_bars` itself.

So these two tests read the SOURCE rather than call it. They are the only
place that checks the callers against the real class and the real import
graph, and they need no broker.
"""
from __future__ import annotations

import ast
import pathlib
import re

import alpaca_cli

HERE = pathlib.Path(__file__).resolve().parent

# `cli.foo(`, `self.cli.foo(`, `self._cli.foo(` - the three ways this repo
# reaches the broker. Attribute reads without a call are excluded on
# purpose: `cli.foo` alone is a bound method being passed around, and a
# test double is a legitimate receiver for that.
CALL = re.compile(r"\b(?:self\.)?_?cli\.([a-z_][a-z0-9_]*)\s*\(")

# Names on the receiver that are NOT AlpacaCli methods: the private
# helpers of the class itself, and the attribute a stub may define.
EXEMPT = {"_run", "_load_keys_from_file"}


def _sources() -> list[pathlib.Path]:
    return sorted(p for p in HERE.glob("*.py")
                  if p.name not in {"alpaca_cli.py", pathlib.Path(__file__).name}
                  and not p.name.startswith("test_"))


def test_every_cli_call_in_the_repo_exists_on_the_class():
    """Every `cli.<name>(` in non-test code must be a real AlpacaCli method.

    This is the check that fails on the day someone deletes a method, in
    the second it happens, instead of five minutes later in a scheduled
    task whose log nobody is watching.
    """
    missing: list[str] = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for name in CALL.findall(line):
                if name in EXEMPT or hasattr(alpaca_cli.AlpacaCli, name):
                    continue
                missing.append(f"{path.name}:{lineno}  cli.{name}(")
    assert not missing, (
        "these calls have no method on AlpacaCli:\n  " + "\n  ".join(missing))


def test_the_check_would_have_caught_the_deletion():
    """The guard is only worth having if it fails on the real defect."""
    assert not hasattr(alpaca_cli.AlpacaCli, "activities_that_never_existed")
    assert CALL.findall("        bookings = cli.activities(after=x)") == \
        ["activities"]
    assert CALL.findall("    body = self.cli.submit_mleg_limit(a, b)") == \
        ["submit_mleg_limit"]
    # and it must not fire on an ordinary attribute read
    assert CALL.findall("        fn = cli.activities") == []


def _import_graph(entry: str) -> set[str]:
    """Local modules reachable from `entry`, transitively."""
    seen: set[str] = set()
    todo = [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = HERE / f"{name}.py"
        if not path.is_file():
            continue
        seen.add(name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def test_the_live_agent_imports_no_module_hardcoding_a_workstation_path():
    """Nothing the agent can reach may carry an absolute path to one machine.

    The agent's own paths come from config.yaml - cli_path and env_file -
    which is why the same code runs on the workstation and on the trading
    host. A module-level constant like

        ENV_FILE = "C:/Users/HMz/Documents/Source/McpServer/.../env.txt"

    breaks that silently: it imports fine, it passes every test that
    monkeypatches it, and it raises FileNotFoundError only on the host,
    only in the rarely-taken branch, only while the market is open.
    """
    absolute = re.compile(r"""["'](?:[A-Za-z]:[/\\]|/(?:home|Users)/)[^"']*["']""")
    offenders: list[str] = []
    for name in sorted(_import_graph("agent")):
        path = HERE / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:                    # module level only
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Constant) and \
                        isinstance(node.value.value, str) and \
                        absolute.fullmatch(f'"{node.value.value}"'):
                    offenders.append(
                        f"{path.name}:{node.lineno}  {target.id} = "
                        f"{node.value.value!r}")
    assert not offenders, (
        "module-level absolute paths reachable from agent.py:\n  "
        + "\n  ".join(offenders))


def test_the_settlement_path_goes_through_the_configured_cli():
    """settlement_close must use self.cli, not a module-level fetcher."""
    src = (HERE / "agent.py").read_text(encoding="utf-8")
    body = src[src.index("    def settlement_close("):]
    body = body[:body.index("\n    def ", 1)]
    assert "self.cli.daily_bars(" in body, body
    assert "fetch_bars" not in src, "the workstation-bound fetcher is back"
