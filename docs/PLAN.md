# Private AI Agent Template — Implementation Plan

Progress: Phases 0 to 4 built. Phase 1 and 2 were verified with stand-in servers; real-model, outside port-scan, host firewall and tcpdump checks are pending on the GPU host. Phase 3 was measured offline with a lexical stand-in embedder; faithfulness and calibration of the real embedder are pending on the GPU host. Phase 4 gateway enforcement is fully tested; the OpenClaw configuration and the model's tool-use score are unverified until it runs on the GPU host. Phase 5 not started. Each phase stops at a review checkpoint.

---

# Phase 0 plan — model selection + one-pager

## Context
PLAN.md describes a self-hosted, privacy-first AI agent template built in phases. The workspace is empty, so this is Phase 0: pick two open-weight models (small daily driver, larger on-demand), write `docs/ONEPAGER.md`, and stop for review. No code this phase.

Decisions already made with the user:
- **Deployment target: a rented GPU VM** (user's choice), not this Mac. The Mac (M3 Pro, 18 GB) stays the dev box.
- **Web verification allowed.** Done; findings below supersede my June-2026 training data.
- **`git init` now**, with `.gitignore` excluding secrets from the first commit.

## Research findings (verified Sept 2026, primary sources where reachable)

| Model | Params / arch | Ctx | Licence | ~Size @4-bit | Tool calling | Serving |
|---|---|---|---|---|---|---|
| Qwen3.5-9B (Feb 2026) | 9B dense | 256K | Apache 2.0 | 6.6 GB (Ollama) | native, Hermes-style `<tool_call>` | Ollama ✓ vLLM ✓ (`--tool-call-parser qwen3_coder --reasoning-parser qwen3`) |
| Qwen3.6-27B (Apr 2026) | 27B dense | 256K (1M YaRN) | Apache 2.0 | 18 GB (Ollama) | native, same format | Ollama ✓ vLLM ✓, documented flags |
| Qwen3.8-27B (Aug 14 2026) | 27B dense | 262K | Apache 2.0 | ~17 GB | native | single secondary source only — re-check |
| Qwen3.5/3.6-35B-A3B | 35B MoE, 3B active | 256K | Apache 2.0 | 23–24 GB | native | too big for 24 GB card |
| Muse Glimmer 30B (Meta, Aug 2026) | 30B dense + 1.8B vision | 131K | Apache 2.0 | <20 GB | tool-use specialist: MCP-Atlas 75.5 vs Qwen3.6-27B 62.5 | Ollama/vLLM claimed; vLLM parser undocumented; 5 weeks old |
| Gemma 4 31B (Apr 2026) | 31B dense | 256K | Apache 2.0 (new for Gemma) | 20 GB | native, τ2-bench 86.4 | Ollama ✓ vLLM ✓ |
| Gemma 4 E4B | 8B total / 4.5B active | 128K | Apache 2.0 | 9.6 GB | native | Ollama ✓ |
| Hermes 4 14B (Aug 2025) | Qwen3-14B base | n/s | Apache 2.0 | ~9 GB | Hermes `<tool_call>`, vLLM `hermes` parser | vLLM ✓, GGUF community |
| Hermes 4.3 36B (Dec 2025) | Seed-OSS-36B base | n/s | Apache 2.0 | 21.8 GB Q4_K_M | Hermes format | vLLM ✓; no KV headroom on 24 GB |
| gpt-oss-20b (Aug 2025) | 21B MoE, 3.6B active | 128K | Apache 2.0 | 14 GB | harmony format; needs XML/`openai` parser workarounds | Ollama ✓ vLLM ✓ |
| Mistral Small 4 (Mar 2026) | 119B MoE, 6B active | 256K | Apache 2.0 | needs 4×H100 | yes | out of budget |
| Llama 4 Scout/Maverick | 109B / 400B MoE | — | Llama 4 Community (700M MAU cap, EU vision bar) | too big | — | rejected |
| DeepSeek V4-Flash, Kimi K2, Qwen3.8-Max | 284B+ | — | MIT / custom | multi-GPU | — | rejected |

OpenClaw (verified via GitHub raw docs; docs site unreachable): MIT, TypeScript, has its own "Gateway" control plane (naming clash with our FastAPI gateway — one-pager must disambiguate). Custom OpenAI-compatible provider via `models.providers.<id>` with `api: "openai-completions"`, `baseUrl`, plus model allowlist in `agents.defaults.models`. Tool policy supports `tools.allow/deny`, groups (`group:fs`, `group:runtime`, `group:web`) and a documented read-only pattern (`profile: minimal`, deny fs/runtime). Runs headless via `openclaw agent --message ...`; Dockerfile + compose provided.

GPU rental (Sept 2026, verify at purchase): RTX 4090 24 GB ≈ $0.34–0.69/h (RunPod), L40S 48 GB ≈ $0.39+/h (RunPod), A100 80 GB ≈ $1.2–2.1/h. Vast.ai is peer-to-peer with no SLA and untrusted hosts — unsuitable for private data.

## Decisions to put in the one-pager

**Daily driver: Qwen3.5-9B** (Ollama tag `qwen3.5:9b`). Apache 2.0, 256K context, ~6.6 GB, native Hermes-style tool calls (exactly what Phase 4 asks for), mature on both Ollama and vLLM, and it also fits the 18 GB Mac for local dev. Fallback if its tool-call parsing proves flaky in Phase 4 tests: Hermes 4 14B (same format, Apache 2.0).

**On-demand: Qwen3.6-27B** (Ollama tag `qwen3.6:27b`). Apache 2.0, dense, 256K, 18 GB at 4-bit, SWE-bench Verified 77.2, documented vLLM flags. Config-only swap to Qwen3.8-27B once its Ollama/vLLM availability is confirmed. Closest rejected alternatives and why: Muse Glimmer 30B (better tool benchmarks but 5 weeks old, parser undocumented — re-evaluate in Phase 4), Gemma 4 31B (loses to Qwen3.6-27B on coding/reasoning and to Glimmer on tool use), Hermes 4.3 36B and 35B-A3B MoEs (no KV headroom on 24 GB).

**GPU class: one 24 GB card** (RTX 4090 / L4 / A10G, dedicated-tenancy tier such as RunPod Secure Cloud or Lambda). Both models cannot co-reside on 24 GB, so the on-demand model is loaded on request; Ollama does this unload/load automatically. If concurrent residency or higher throughput is wanted, step up to a 48 GB L40S and run two vLLM containers. Document both.

**Serving: Ollama behind its OpenAI-compatible `/v1`** for Phase 1 (same runtime on Mac dev and GPU box, automatic model swapping, native tool calling for both picks). vLLM as a second Compose profile for throughput later; keep model ids and base URL in `infra/config.yaml` so the swap is config-only.

**Frontier lane:** Claude via API, default `claude-sonnet-5`, `claude-opus-5` selectable in config. Exact ids re-checked with the claude-api skill in Phase 2.

**Rented-GPU privacy caveats (must be in the one-pager):** private data sits on a third party's disk — dedicated tenancy only, encrypted data volume, Tailscale SSH only, default-deny inbound firewall, gateway-only egress allowlist, and the box is destroyed/wiped when not in use. Whole stack (gateway, model, RAG, agent) runs on the box; the Mac is a Tailscale client.

**Staleness / re-check list:** Qwen3.8-27B (one source), Muse Glimmer vLLM parser and Ollama tag, Qwen3.5-9B tool-call reliability under Ollama (OpenClaw docs mention Qwen sometimes emits raw-text tool calls — `tool_choice: required` workaround), Hermes 4 14B context length, exact licence text on each HF card, GPU prices, OpenClaw config schema (docs site was down; used GitHub raw + third-party mirror).

## Steps (Phase 0 only)
1. `git init` in the project folder; create `.gitignore` (`.env`, `*.key`, `infra/secrets/`, `__pycache__`, `.venv`, model caches). Commit: "chore: init repo".
2. Create the repo skeleton dirs from PLAN.md (`gateway/ agent/ rag/ infra/ tests/ docs/`) with `.gitkeep` only — no code. Commit: "chore: repo layout".
3. Write `docs/ONEPAGER.md` with sections: Purpose; Chosen models (with reasoning and the comparison table above); Rejected alternatives and why; Where knowledge may be stale / re-check before committing; Stack (OpenClaw chassis, Ollama→vLLM, FastAPI gateway, Docker Compose, Tailscale); Deployment target and GPU sizing (24 GB vs 48 GB); Privacy caveats of a rented GPU; What week one produces (Phase 1 + Phase 2 deliverables and their acceptance tests). No client, company or individual names — "Private AI Agent Template" only. Commit: "docs: phase 0 one-pager".
4. Summarise and STOP for review per PLAN.md checkpoint.

## Verification (Phase 0)
- `git log` shows three small commits; `git status` clean; `.env` pattern present in `.gitignore`.
- `grep -ri` over docs/ for the user's name, email, or any company string returns nothing.
- One-pager answers every Phase 0 bullet: two models, evaluation criteria (tool use, context, VRAM, licence, serving maturity), rejected list, stale-knowledge section, stack, week-one output.

---

# Phases 1–5 — design (each phase ends at a CHECKPOINT; nothing below starts until the previous phase is reviewed)

## Cross-cutting architecture

```
Mac (Tailscale client) ──ssh/tailnet──▶ GPU VM (Tailscale node, ufw default-deny inbound)
                                          │
                                          ├─ gateway   (FastAPI, :8080 bound to 127.0.0.1 + tailscale0)
                                          │    ├─ taint engine   gateway/taint.py
                                          │    ├─ lane router    gateway/lanes.py   (private | frontier)
                                          │    ├─ egress()       gateway/egress.py  (single chokepoint)
                                          │    ├─ permissions    gateway/permissions.py (read-only tool gate)
                                          │    ├─ audit log      gateway/audit.py   (JSONL, hashes only)
                                          │    └─ users/sessions gateway/session.py (per-user_id namespaces)
                                          ├─ model    (Ollama, OpenAI-compat /v1, internal network only)
                                          ├─ rag      (Phase 3; chromadb-or-sqlite behind rag/store.py interface)
                                          └─ agent    (OpenClaw container, model provider = gateway URL)
```
- Two Compose networks: `internal` (no external route: `internal: true`) for model/rag/agent; gateway is the only service also on the default bridge. Egress allowlist enforced with an iptables/nftables rule in the gateway container entrypoint (OUTPUT default DROP except DNS + `api.anthropic.com:443`), plus Compose `internal: true` for everything else.
- All config in `infra/config.yaml` (pydantic-settings loader `gateway/config.py`): users, sources registry, models (daily/on-demand ids, base URL), frontier (`auto_route_clean`, `consent_scope`, model id), tool allowlist.
- Test harness: pytest under `tests/`; `make test` runs all. Every phase adds tests, never deletes.
- Python deps to propose (ask-before-add rule applies to network-calling deps): fastapi, uvicorn, httpx (network: to Ollama + Anthropic), pydantic, pyyaml, pytest, respx (mock httpx). Phase 3 adds chromadb + sentence-transformers or a local embedding via Ollama `/api/embed` (no new network dep). I will ask before adding `anthropic` SDK vs plain httpx (plan: plain httpx, one fewer dep).

## Phase 1 — Skeleton + local model
Files: `infra/docker-compose.yml`, `infra/config.yaml`, `infra/.env.example`, `Makefile`, `gateway/{main.py,config.py,model_client.py}`, `gateway/Dockerfile`, `tests/test_phase1.py`.
- Compose services: `ollama` (GPU passthrough via `deploy.resources.reservations.devices`; on Mac dev a `mac-dev` profile instead points gateway at `host.docker.internal:11434` and Ollama runs natively) and `gateway`. Ollama pulls `qwen3.5:9b` and `qwen3.6:27b` on first `make up` via an init job.
- Gateway endpoints: `GET /health` (checks Ollama `/api/tags`), `POST /v1/chat/completions` (proxy to Ollama, streaming passthrough), `GET /v1/models`. Bound with `--host 127.0.0.1` in Compose port mapping `127.0.0.1:8080:8080`; Tailscale access is via the host's tailscale0 (documented in RUNBOOK; no 0.0.0.0).
- Model id comes from config; `X-Model: on-demand` header (or `model` field alias) selects the 27B.
- Accept: `curl localhost:8080/v1/chat/completions` returns completion; `nmap` from another host over public IP shows no open ports except SSH (documented procedure + test script `tests/test_portscan.sh`).

## Phase 2 — Taint engine, two-lane gateway, audit log (taint engine first)
Files: `gateway/taint.py`, `gateway/fragments.py`, `gateway/session.py`, `gateway/lanes.py`, `gateway/egress.py`, `gateway/audit.py`, `gateway/frontier_client.py`, `infra/egress-allowlist.sh`, `tests/privacy/*`.
- `Taint` enum `CLEAN < PRIVATE`; `Fragment(text, taint, origin, turn)`; `Context = list[Fragment]`, `context_taint() = max(...)`. Sources registry in config; `taint_for_source(name)` returns PRIVATE unless explicitly `CLEAN`.
- Propagation rules implemented as pure functions: retrieval → doc taint; tool output → `max(tool taint, args taint)`; model output → context taint; history → fragments persist in session across turns. Only `/new` (`POST /session/new`) clears.
- `egress(session, request)` in `egress.py` is the only place `frontier_client` is called. Order: (1) taint check → raise `TaintBlocked`; (2) consent: if `auto_route_clean=false` need `session.consent` (scope `session`) or per-request `consent=true` (scope `request`); else auto; (3) build frontier payload from user turn only, no fragments with origin != user, `attachments` present → reject; (4) write audit record `{ts, lane, user_id, prompt_sha256, tokens_in, tokens_out, destination, consent_mode, auto_route_clean}`; (5) call.
- Response always includes `lane: "private"|"frontier"`; frontier offer surfaced as `frontier_available: true` when CLEAN and consent missing.
- Local model failure → HTTP 503 `{"lane":"private","error":"local_model_unavailable"}`; no fallback path exists in code.
- Egress allowlist: gateway entrypoint installs iptables OUTPUT rules; model/rag/agent on `internal: true` network.
- Tests (tests/privacy): canary corpus (200 mixed queries, respx-mocked Anthropic endpoint asserting no canary in any request body); taint-overrides-consent; N+3 history propagation; consent false/true paths + audit written; audit count == outbound count (pytest counts respx calls; plus `tests/privacy/tcpdump_check.sh` for the live check); kill-model-fails-closed (stop ollama container → expect 503, zero frontier calls); prompt-injection doc marked "not sensitive" keeps PRIVATE.

## Phase 3 — RAG
Files: `rag/{ingest.py,chunk.py,embed.py,store.py,retrieve.py}`, `rag/store_chroma.py`, `tests/rag/{golden.jsonl,test_rag.py,report.py}`.
- `VectorStore` Protocol in `store.py` (`add(chunks, user_id)`, `query(text, user_id, k, filter)`); Chroma implementation with `user_id` as collection namespace AND metadata filter (belt and braces).
- Embeddings via Ollama `/api/embed` with a config-selected model (e.g. `nomic-embed-text` or `qwen3-embedding`; re-check availability) — keeps the internal network rule intact.
- Chunks carry `source`, `taint` (from registry, default PRIVATE), `user_id`; retrieval returns Fragments, wired only into the private lane builder.
- Accept: 50 golden Q/A + 10 no-answer over a seeded doc set in `tests/rag/corpus/`; `report.py` prints recall@5, MRR, faithfulness (local-model judge, private lane), no-answer rate; cross-user isolation test (User A query never returns B chunks).

## Phase 4 — Agent + permission layer
Files: `agent/openclaw.json` (provider = gateway `http://gateway:8080/v1`, `api: openai-completions`, model `qwen3.5:9b` allowlisted; tools `profile: minimal`, allow `read`, custom `search`/`context_query` plugin tools, deny `group:fs` writes, `group:runtime`, `group:web`), `agent/tools/*.py` (thin plugin tools that call gateway endpoints), `gateway/permissions.py`, `tests/security/{injection_corpus/,test_injection.py}`, `tests/agent/{tasks.jsonl,test_tools.py}`.
- Gateway enforces read-only independently of OpenClaw config: a static allowlist of tool names + arg validators; any other tool call in a model response is stripped and logged; `write/exec/edit/apply_patch/web_*` are rejected with 403.
- Tool results returned as Fragments with taint = max(tool source taint, argument taint).
- Accept: 20-task set scored on tool name, args, stop condition; write attempts blocked; injection corpus cannot change lane, taint, or exfiltrate (assert zero frontier calls and no canary).

## Phase 5 — Packaging + results
Files: `Makefile` (`up`, `down`, `test`, `pull-models`), `docs/RUNBOOK.md`, `docs/TEST_RESULTS.md`, `README.md`, `infra/.env.example`.
- README "Routing behaviour" section with the exact table from PLAN.md plus the taint-overrides-everything statement, default `auto_route_clean: false`, and `/new` note.
- Name scrub: `grep -rEi "<user name>|<email>|<company>" .` must return nothing; only "Private AI Agent Template".
- `make test` runs privacy, rag, agent, security suites and writes TEST_RESULTS.md rows.
