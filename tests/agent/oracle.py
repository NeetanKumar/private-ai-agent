"""Scripted stand-in models for testing the scorer. They know the task set; real models do not."""
import json
from typing import Any, Dict, List


def _step_for(tasks, messages):
    prompt = next(m["content"] for m in messages if m["role"] == "user")
    task = next(t for t in tasks if t["prompt"] == prompt)
    done = sum(1 for m in messages if m["role"] == "tool")
    return task, task["steps"][done]


def oracle_model(tasks):
    """Always does the right thing."""
    async def model(messages: List[Dict[str, Any]], tools):
        _, step = _step_for(tasks, messages)
        if "call" in step:
            args = {k: (v["contains_any"][0] if isinstance(v, dict) else v) for k, v in step["call"]["args"].items()}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": step["call"]["tool"], "arguments": json.dumps(args)}}]}
        words = step["final"]["contains_any"]
        return {"role": "assistant", "content": f"Here you go: {words[0]}"}
    return model


def wrong_tool_model(tasks):
    async def model(messages, tools):
        _, step = _step_for(tasks, messages)
        if "call" in step:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}
        return {"role": "assistant", "content": step["final"]["contains_any"][0]}
    return model


def write_happy_model(tasks):
    """Tries to write or execute whatever it is asked."""
    async def model(messages, tools):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "write_file",
                                                          "arguments": json.dumps({"path": "a", "content": "b"})}}]}
    return model


def never_stops_model(tasks):
    async def model(messages, tools):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}
    return model
