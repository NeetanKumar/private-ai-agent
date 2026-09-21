# Private AI Agent Template

A self-hosted, privacy-first AI agent. Your data is answered by a local open-weight model. A second
lane can call Claude through the commercial API, but only for context that is provably clean, and by
default only after you say yes. Nothing listens on a public interface.

Routing is mechanism, not judgment. A deterministic taint check decides what may leave. No model and
no classifier ever makes a routing decision.

## Routing behaviour

| Context taint | `auto_route_clean` | Result |
|---|---|---|
| CLEAN   | false | Local answer, frontier offered — egress only on explicit consent |
| CLEAN   | true  | Routes to frontier automatically, audit record written |
| PRIVATE | false | Local only. No offer, no egress. |
| PRIVATE | true  | Local only. No offer, no egress. |

Taint always overrides consent and config. A PRIVATE context can never reach the frontier lane,
regardless of `auto_route_clean`, flags, or user action.

The default is `auto_route_clean: false`. `/new` is the only way to clear taint.

Every response says which lane served it (`lane` in the body, `X-Lane` header).

## How taint works

- Context is a list of tagged fragments, never one concatenated string. Context taint is the
  maximum over its fragments.
- Each source is labelled in `infra/config.yaml`. A source that is not listed is PRIVATE. Marking
  anything CLEAN needs an explicit entry.
- Taint follows retrieval, tool output (the tool's taint and the taint of its arguments), model
  output (the taint of the whole context it was written from), and conversation history.
- Nothing lowers taint except `/new`. There is no topic-change heuristic, no decay, and no model
  judgment.
- All outbound frontier traffic goes through one function. It checks taint, then consent, then sends
  only the current user message, rejects attachments, and writes an audit record.

## Quick start

Prerequisites: Docker with Compose, Python 3.9 or newer for the tests, and an Ollama server (in a
container on a GPU host, or native on a laptop).

```bash
make init        # creates infra/.env with a random gateway token
make up          # GPU host: gateway + Ollama container
make models      # pull the models named in infra/config.yaml (first time only)
```

On a laptop without an NVIDIA GPU, run Ollama natively and use `make up-mac` and `make models-mac`.

The gateway is published on `127.0.0.1:8080` only. Reach a remote host with an SSH tunnel or over
Tailscale. `docs/RUNBOOK.md` covers a full rented-GPU deployment, the outbound firewall, and how to
verify it.

```bash
TOKEN=$(grep GATEWAY_TOKEN_OWNER infra/.env | cut -d= -f2)

curl -s localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

## Test UI

The gateway serves a small browser UI at `/ui`. Open `http://localhost:8080/ui` (through your SSH
tunnel if the gateway is remote), paste your token, and chat. It shows, on every reply, which lane
answered and the taint, plus the session state, the consent controls, a frontier offer with a
confirmation step, your own files and document tools, and your own audit and security records.

It is served with a strict content policy: no outside requests, no images, and every reply is shown
as plain text. Set `ui.enabled: false` in `infra/config.yaml` to serve no UI. It does not ingest
documents; use `make ingest` for that.

## Using it

| Want to | Do this |
|---|---|
| Ask a question | `POST /v1/chat/completions` with a normal OpenAI-style body. |
| Choose the larger model | `"model": "on-demand"`. Default is `daily`. |
| Give consent to the frontier (session scope) | `POST /session/consent {"consent": true}`, or `"consent": true` in a chat request. |
| Give consent for one request (request scope) | `"consent": true` in that request only. |
| Force the local model | `"lane": "private"`. |
| Add private context | `"context": [{"text": "...", "source": "my_source"}]`. Taint comes from the registry, never from the request. |
| Ask about your documents | `"documents": true`. Private lane only. |
| Start over and clear taint | Send exactly `/new`, or `POST /session/new`. |
| See session state | `GET /session`. |
| Add a user | Add an entry under `users:` in `infra/config.yaml` and set its token variable. No code change. |

## Your documents

Put files in `data/inbox/<user id>/`, then ingest them. Markdown, text and PDF are supported.

```bash
make ingest USER_ID=owner SOURCE=my_notes FILES="/inbox/owner/notes.md /inbox/owner/report.pdf"
make docs-list USER_ID=owner
```

Chunks carry the taint of their source. Retrieval is filtered by user, so one user can never
retrieve another's chunks. A question with `documents: true` is answered by the local model only,
and when nothing relevant is found the answer is `not in documents`.

## Agent

The agent loop is OpenClaw's; this project does not write one. The gateway is the model endpoint and
also the permission layer. Only four read-only tools exist (list files, read a file, search files,
query documents). Calls to anything else are removed before they leave the gateway, whatever the agent
or the model asks for. See `agent/README.md`.

## Security model

| Control | Where it is enforced |
|---|---|
| Nothing public | Ports bind to loopback or a tailnet address. Other containers sit on an internal network with no route out. |
| Only the gateway can go out | A host firewall rule limits the gateway's network to the frontier API host (`infra/egress-allowlist.sh`). |
| Taint cannot be overridden | Deterministic code in the gateway. Consent, flags and config are checked after it. |
| Local model down means an error | The private lane has no path to the frontier. |
| Tools are read-only | An allowlist in code. Config can narrow it, never extend it. |
| Prompt injection | Documents and tool output reach the model only as delimited, untrusted data. They cannot set taint, lane, consent or tool permissions. |
| Auto-loading exfiltration | Images and active HTML are stripped from replies. |
| Logs | The audit log holds hashes and counts only. No prompt bodies and no user data are logged. |
| Secrets | `infra/.env` is git-ignored and created with mode 600. |

## Testing

```bash
make test        # runs every suite and rewrites docs/TEST_RESULTS.md
```

`docs/TEST_RESULTS.md` lists each check, how it was run, and whether it passed. It also lists the
checks that can only be done on a real GPU host and marks them not run.

## Status

The gateway, taint engine, audit log, retrieval and permission layer are built and covered by
automated tests that use stand-in servers. The following need a real GPU host and are listed as
pending in `docs/TEST_RESULTS.md`:

- the outside port scan, the packet-capture check, and the host firewall rules
- a real model's answers, including the four near-miss "not in documents" questions that a score
  floor cannot catch
- tool-use scoring, retrieval quality with a real embedding model, and faithfulness
- a live frontier call, and running OpenClaw with the provided configuration

## Repository layout

```
gateway/   lanes, taint engine, egress chokepoint, permissions, audit log, tools
rag/       ingest, chunking, embedding, storage interface, retrieval
agent/     OpenClaw configuration and the read-only tool bridge
infra/     Docker Compose, config.yaml, firewall script, env example
tests/     privacy, security, agent and retrieval suites
scripts/   first-run setup and the test harness
docs/      ONEPAGER.md, RUNBOOK.md, TEST_RESULTS.md, PLAN.md
```
