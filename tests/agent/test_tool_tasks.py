import asyncio
import json
import pathlib
import sys

import pytest

import oracle
import tool_eval
from gateway.config import get_settings
from run_eval import fixtures


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return run(fixtures(tmp_path_factory.mktemp("eval")))


@pytest.fixture(scope="module")
def system():
    return tool_eval.system_prompt(get_settings())


def test_there_are_twenty_well_formed_tasks():
    tasks = tool_eval.load_tasks()
    assert len(tasks) == 20 and len({t["id"] for t in tasks}) == 20
    for t in tasks:
        assert t["steps"] and "final" in t["steps"][-1]                  # every task ends in a stop condition
        assert all("call" in s for s in t["steps"][:-1])
        for s in t["steps"]:
            if "call" in s:
                assert s["call"]["tool"] in tool_eval.ENABLED


def test_tasks_are_solvable_from_the_real_tool_outputs(ctx):
    """Guard against an unanswerable task: the expected answer must appear in what the tools return."""
    from gateway.tools import ToolError, execute
    for t in tool_eval.load_tasks():
        calls = [s["call"] for s in t["steps"] if "call" in s]
        final = t["steps"][-1]["final"]
        if not calls or final.get("no_tool"):
            continue
        out = ""
        for c in calls:
            args = {k: (v["contains_any"][0] if isinstance(v, dict) else v) for k, v in c["args"].items()}
            try:
                out += "\n" + run(execute(c["tool"], args, ctx, tool_eval.ENABLED))
            except ToolError as e:
                out += f"\nerror: {e}"
        assert any(w.lower() in out.lower() for w in final["contains_any"]), t["id"]


def test_a_correct_model_scores_twenty_of_twenty(ctx, system):
    tasks = tool_eval.load_tasks()
    rep = run(tool_eval.run_eval(oracle.oracle_model(tasks), tasks, ctx, system))
    assert sum(rep.tasks.values()) == 20, rep.summary()
    assert rep.rate("call", "tool_ok") == rep.rate("call", "args_ok") == rep.rate("final", "stop_ok") == 1.0
    assert rep.blocked_by_gateway == 0


def test_a_model_that_picks_the_wrong_tool_is_scored_down(ctx, system):
    tasks = tool_eval.load_tasks()
    rep = run(tool_eval.run_eval(oracle.wrong_tool_model(tasks), tasks, ctx, system))
    assert rep.rate("call", "tool_ok") < 0.3 and sum(rep.tasks.values()) < 8


def test_a_model_that_never_stops_fails_every_stop_condition(ctx, system):
    tasks = tool_eval.load_tasks()
    rep = run(tool_eval.run_eval(oracle.never_stops_model(tasks), tasks, ctx, system))
    assert rep.rate("final", "stop_ok") == 0.0 and sum(rep.tasks.values()) == 0


def test_every_write_attempt_is_blocked_and_counted(ctx, system):
    tasks = tool_eval.load_tasks()
    rep = run(tool_eval.run_eval(oracle.write_happy_model(tasks), tasks, ctx, system))
    total_steps = sum(len(t["steps"]) for t in tasks)
    assert rep.blocked_by_gateway == total_steps                   # nothing got through, not even once
    assert sum(rep.tasks.values()) == 0
    assert not (pathlib.Path(ctx.files_root) / "eval" / "a").exists()        # and nothing was written


def test_argument_matching_rules():
    m = tool_eval.args_match
    assert m({"path": "a.md"}, {"path": "a.md"}) and not m({"path": "a.md"}, {"path": "b.md"})
    assert m({"query": {"contains_any": ["Lisbon"]}}, {"query": "the lisbon move"})
    assert not m({"query": {"contains_any": ["Lisbon"]}}, {"query": "paris"})
    assert not m({"path": "a.md"}, {"path": "a.md", "mode": "w"})           # unexpected argument
