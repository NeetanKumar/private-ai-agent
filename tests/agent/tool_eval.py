"""Scores a model's tool use one decision at a time (teacher forcing), so no tool-calling loop is
written here. Each task is a list of expected steps. For step i the model is shown the prompt plus
the CORRECT earlier calls and their real tool results, and we score only its next action:

  call step   right tool, right arguments
  final step  no further tool call (the stop condition) and an answer containing the expected text

A task passes only if every step passes. The same permission check the gateway applies to model
output is applied to each response first, so a blocked call counts as a wrong action."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from gateway.permissions import enforce_tool_calls
from gateway.tools import READ_ONLY_TOOLS, ToolContext, ToolError, execute

HERE = pathlib.Path(__file__).resolve().parent
TASKS = HERE / "tasks.jsonl"
ENABLED = list(READ_ONLY_TOOLS)
TOOL_DEFS = [READ_ONLY_TOOLS[n].definition() for n in ENABLED]

Model = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Awaitable[Dict[str, Any]]]


def load_tasks() -> List[dict]:
    return [json.loads(l) for l in TASKS.read_text().splitlines() if l.strip()]


def args_match(expected: Dict[str, Any], actual: Dict[str, Any]) -> bool:
    if set(actual) - set(expected) - {"top_k"}:
        return False
    for k, want in expected.items():
        got = actual.get(k)
        if isinstance(want, dict):                       # {"contains_any": [...]}
            if not isinstance(got, str) or not any(w.lower() in got.lower() for w in want["contains_any"]):
                return False
        elif got != want:
            return False
    return True


@dataclass
class StepResult:
    task: str
    index: int
    kind: str                     # call | final
    tool_ok: bool = False
    args_ok: bool = False
    stop_ok: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.tool_ok and self.args_ok if self.kind == "call" else self.stop_ok


@dataclass
class Report:
    steps: List[StepResult] = field(default_factory=list)
    tasks: Dict[str, bool] = field(default_factory=dict)
    blocked_by_gateway: int = 0

    def rate(self, kind: str, attr: str) -> float:
        xs = [getattr(s, attr) for s in self.steps if s.kind == kind]
        return sum(xs) / len(xs) if xs else 1.0

    @property
    def task_pass_rate(self) -> float:
        return sum(self.tasks.values()) / len(self.tasks)

    def summary(self) -> str:
        lines = [f"tasks passed        : {sum(self.tasks.values())}/{len(self.tasks)}",
                 f"correct tool        : {self.rate('call', 'tool_ok'):.3f}",
                 f"correct arguments   : {self.rate('call', 'args_ok'):.3f}",
                 f"correct stop/answer : {self.rate('final', 'stop_ok'):.3f}",
                 f"calls blocked by the gateway permission check: {self.blocked_by_gateway}"]
        for s in self.steps:
            if not s.ok:
                lines.append(f"  FAIL {s.task} step {s.index} ({s.kind}): {s.detail}")
        return "\n".join(lines)


def system_prompt(cfg) -> str:
    return cfg.models.system_prompt + " " + cfg.tools.system_prompt


async def run_eval(model: Model, tasks: List[dict], ctx: ToolContext, system: str) -> Report:
    rep = Report()
    for t in tasks:
        history: List[Dict[str, Any]] = [{"role": "system", "content": system},
                                         {"role": "user", "content": t["prompt"]}]
        passed = True
        for i, step in enumerate(t["steps"]):
            msg = await model(list(history), TOOL_DEFS)
            kept, blocked = enforce_tool_calls(msg, set(ENABLED), ENABLED, "eval", None, 4)
            rep.blocked_by_gateway += blocked
            if "call" in step:
                exp = step["call"]
                r = StepResult(t["id"], i, "call")
                if kept:
                    first = kept[0]["function"]
                    r.tool_ok = first["name"] == exp["tool"]
                    r.args_ok = r.tool_ok and args_match(exp["args"], json.loads(first["arguments"]))
                    r.detail = f"got {first['name']}({first['arguments']}), want {exp['tool']}({exp['args']})"
                else:
                    r.detail = f"no tool call (blocked={blocked}), want {exp['tool']}"
                rep.steps.append(r)
                passed &= r.ok
                # teacher forcing: continue from the CORRECT call and its real result
                args = {k: (v["contains_any"][0] if isinstance(v, dict) else v) for k, v in exp["args"].items()}
                cid = f"call_{t['id']}_{i}"
                try:
                    out = await execute(exp["tool"], args, ctx, ENABLED)
                except ToolError as e:
                    out = f"error: {e}"
                history += [{"role": "assistant", "content": "", "tool_calls": [
                                {"id": cid, "type": "function",
                                 "function": {"name": exp["tool"], "arguments": json.dumps(args)}}]},
                            {"role": "tool", "tool_call_id": cid,
                             "content": f'<tool_output name="{exp["tool"]}">\n{out}\n</tool_output>'}]
            else:
                exp = step["final"]
                r = StepResult(t["id"], i, "final")
                text = (msg.get("content") or "").lower()
                no_tool = not kept and blocked == 0
                r.stop_ok = no_tool and any(w.lower() in text for w in exp["contains_any"])
                r.detail = (f"tool call issued/attempted: {[c['function']['name'] for c in kept] or blocked}"
                            if not no_tool else f"answer lacked any of {exp['contains_any']}: {text[:80]!r}")
                rep.steps.append(r)
                passed &= r.ok
        rep.tasks[t["id"]] = passed
    return rep


def http_model(base_url: str, model_id: str, timeout: float = 300) -> Model:
    """A real OpenAI-compatible server (Ollama or vLLM), called directly at temperature 0."""
    async def call(messages, tools):
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(base_url.rstrip("/") + "/chat/completions", json={
                "model": model_id, "messages": messages, "tools": tools, "temperature": 0})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]
    return call
