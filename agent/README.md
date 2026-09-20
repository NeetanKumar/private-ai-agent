# Agent (OpenClaw) for the Private AI Agent Template

The agent loop is OpenClaw's. This project does not implement one. What we provide is the model
endpoint (the gateway), the read-only tools, and the rules the gateway enforces on both.

## What is enforced where

| Layer | Enforced by | Effect |
|---|---|---|
| Which tools exist | the gateway, in code (`gateway/tools.py`) | Only four read-only tools can ever run: `list_files`, `read_file`, `search_files`, `context_query`. Config can turn them off but cannot add another. |
| What the model may call | the gateway, on every model response | Calls to anything else are removed before the response leaves the gateway. Arguments are validated. |
| What the model is shown | the gateway | The agent's own tool definitions are reduced to names. The model sees the gateway's canonical definitions only. |
| Where tools can read | the gateway | One folder per user. `..`, absolute paths and symlinks out of the folder are refused. |
| Taint of tool results | the gateway | Results are PRIVATE unless the operator explicitly registers the tool as CLEAN, and they inherit the taint of the call's arguments. A client cannot set it. |
| Lane | the gateway | Any turn with tools or tool results is private-lane only. |

OpenClaw's own tool policy (`openclaw.json`) is a second, independent layer. If it were
misconfigured, the gateway would still refuse write, exec and web calls.

## Files

- `openclaw.json`: model provider pointing at the gateway, and a read-only tool policy.
- `mcp_server.py`: stdio MCP bridge. It lists and forwards the gateway's tools and holds no logic.

## Status: not yet verified against a running OpenClaw

`openclaw.json` was written from OpenClaw's published configuration docs. Its documentation site
was unreachable while this was written, so parts came from the project's GitHub docs and a
third-party mirror, and the two sources showed two slightly different layouts for the agent
section (`agents.defaults` and `agents.entries`); both are present here. OpenClaw has not been
installed or run. Before relying on it:

1. Check every key against the current OpenClaw docs, or validate with its own config command.
2. Register `mcp_server.py` as an MCP server using OpenClaw's MCP configuration
   (command `python3 agent/mcp_server.py`, environment `GATEWAY_URL` and `GATEWAY_TOKEN`).
3. Confirm OpenClaw's built-in `read`, `write`, `exec` and web tools are all denied.

## Rules for the provider configuration

The provider entry must never set `consent`, `lane` or `documents` in a static request body.
Consent is a human action. If an agent could set it, an injected instruction could too.

## Measuring tool use

`tests/agent/run_eval.py` scores the 20-task set against a real model, one decision at a time:

    python tests/agent/run_eval.py --url http://localhost:11434/v1 --model qwen3.5:9b

`--self-check` runs the harness with a scripted stand-in. That proves the harness, not a model.
