# Private AI Agent Template — One-Pager (Phase 0)

Status: written in Phase 0. The model choices were checked against public sources in September 2026 and should be re-checked before committing to a host. The project is now built; see `TEST_RESULTS.md` for what has and has not been verified.

## Purpose
A self-hosted, privacy-first agent. Private data is answered by a local open-weight model only. A second lane calls Claude through the commercial API, and only for context that is provably clean and, by default, only after explicit consent. Routing is mechanism, not judgment: a deterministic taint check runs first and can never be overridden.

## Chosen models

| Role | Model | Ollama tag | Why |
|---|---|---|---|
| Daily driver | Qwen3.5-9B | `qwen3.5:9b` | Apache 2.0. 256K context. About 6.6 GB at 4-bit. Native Hermes-style `<tool_call>` function calling, which is what the Phase 4 agent needs. Supported on Ollama and vLLM. Also fits an 18 GB laptop for development. |
| On-demand | Qwen3.6-27B | `qwen3.6:27b` | Apache 2.0. Dense. 256K context. About 18 GB at 4-bit. Same tool-call format as the daily driver, so swapping needs no gateway change. Documented vLLM flags (`--tool-call-parser qwen3_coder --reasoning-parser qwen3`). |

Fallback for the daily driver, if tool-call parsing proves unreliable in Phase 4 tests: Hermes 4 14B (Apache 2.0, Hermes tool format, vLLM `hermes` parser).

## Evaluation criteria
Tool-use quality, context length, memory footprint at realistic quantization, commercial licence terms, and serving maturity on Ollama and vLLM.

## Candidates considered

| Model | Licence | ~4-bit size | Verdict |
|---|---|---|---|
| Qwen3.5-9B | Apache 2.0 | 6.6 GB | **Chosen (daily)** |
| Qwen3.6-27B | Apache 2.0 | 18 GB | **Chosen (on-demand)** |
| Qwen3.8-27B (Aug 2026) | Apache 2.0 | ~17 GB | Drop-in upgrade for the on-demand slot once Ollama and vLLM support is confirmed. Only one secondary source so far. |
| Muse Glimmer 30B (Meta, Aug 2026) | Apache 2.0 | under 20 GB | Strongest tool-use benchmark (MCP-Atlas 75.5 vs 62.5 for Qwen3.6-27B), but about five weeks old and no documented vLLM tool parser. Re-evaluate in Phase 4. |
| Gemma 4 31B / E4B (Apr 2026) | Apache 2.0 | 20 GB / 9.6 GB | Solid and now permissively licensed. Behind Qwen3.6-27B on coding and reasoning, behind Glimmer on tool use. No advantage that justifies a third family. |
| Hermes 4 14B / 4.3 36B (Nous) | Apache 2.0 | ~9 GB / 21.8 GB | 14B is the fallback. 4.3 36B leaves almost no KV-cache headroom on a 24 GB card. |
| gpt-oss-20b | Apache 2.0 | 14 GB | Uses the harmony format, which needs parser workarounds in the agent chassis. Older than the Qwen picks. |
| Qwen3.5/3.6-35B-A3B (MoE) | Apache 2.0 | 23-24 GB | Too large for a 24 GB card with usable context. |
| Mistral Small 4 (119B MoE) | Apache 2.0 | needs 4 x H100 | Out of budget. |
| Llama 4 Scout / Maverick | Llama 4 Community | 109B / 400B MoE | Too large. Licence carries a 700M-MAU cap and an EU vision restriction. No small dense Llama 4 exists. |
| DeepSeek V4-Flash, Kimi K2, Qwen3.8-Max | MIT / modified MIT / custom | multi-GPU | Out of budget. Qwen3.8-Max also has a custom revenue-based licence. |

## Where this knowledge may be stale: re-check before committing
- Qwen3.8-27B availability and Ollama/vLLM support (single source).
- Muse Glimmer: Ollama tag and correct vLLM tool-call parser.
- Qwen3.5-9B tool-call reliability under Ollama. OpenClaw documentation notes Qwen models sometimes emit tool calls as plain text; the workaround is `tool_choice: required` per model.
- Hermes 4 14B context length (not stated on its model card).
- Exact licence text on each Hugging Face card, at download time.
- GPU rental prices. They move weekly.
- OpenClaw config schema. Its documentation site was unreachable during research, so details came from GitHub raw docs and one third-party mirror.
- Claude model ids for the frontier lane, to be confirmed in Phase 2.

## Stack
- **Agent chassis:** OpenClaw (MIT, TypeScript). We do not write our own agent loop. It is pointed at our gateway as a custom OpenAI-compatible provider. Note that OpenClaw has its own internal "Gateway" control plane. In this project, "gateway" always means our FastAPI service.
- **Model serving:** Ollama first, exposing an OpenAI-compatible `/v1` API. Same runtime on a laptop and on the GPU host, with automatic model load and unload. A vLLM Compose profile is planned for higher throughput. Model ids and base URL live in `infra/config.yaml`, so the swap is configuration only.
- **Gateway:** FastAPI. Holds the taint engine, lane router, single egress function, read-only permission layer, and audit log. It is the only component with outbound network access.
- **Frontier lane:** Claude through the commercial API, called with plain HTTP from the gateway.
- **Packaging:** Docker Compose, `make up` and `make down`.
- **Access:** Tailscale or SSH tunnel only. Nothing listens on a public interface.

## Deployment target: rented GPU
- **Baseline: one 24 GB card** (RTX 4090, L4 or A10G class), on a dedicated-tenancy tier. Both models cannot be resident together at 24 GB, so the on-demand model loads when requested and Ollama unloads the other.
- **Upgrade: one 48 GB card** (L40S class) if both models should stay loaded, or if throughput matters. Run two vLLM containers.
- Indicative prices as of September 2026: 24 GB about $0.34-0.69 per hour, 48 GB from about $0.39 per hour, 80 GB A100 about $1.2-2.1 per hour. Verify at purchase.
- **Avoid** peer-to-peer marketplaces with no uptime guarantee and unknown hosts. They are unsuitable for private data.

## Privacy caveats of a rented GPU
Private data lives on a third party's hardware. Mitigations, all required:
- Dedicated tenancy only. Encrypted data volume.
- Tailscale or SSH access only. Default-deny inbound firewall.
- Outbound allowlist: only the gateway may reach the frontier API host.
- Model, retrieval and agent services on an internal network with no external route.
- Destroy or wipe the instance when it is not in use.
- No prompt bodies or user data in logs. Audit records hold hashes and counts only.

## What week one produces
**Phase 1: skeleton and local model.**
- Compose stack with gateway and model server.
- Gateway proxies chat completions to the local model, has a health endpoint, and binds to loopback and tailnet only.
- Accepted when a `curl` through the gateway returns a completion and an outside port scan shows nothing open.

**Phase 2: taint engine, two-lane gateway, audit log.**
- Taint engine first: tagged fragments, context taint is the maximum over fragments, unregistered sources default to PRIVATE, only `/new` clears taint.
- One egress function that checks taint, then consent per `auto_route_clean` and `consent_scope`, then strips context, rejects attachments and writes the audit record.
- Config default `auto_route_clean: false`. Both values implemented and tested.
- Private lane fails closed if the local model is down.
- Accepted by the privacy suite: canary strings never reach the frontier across 200 mixed queries, taint overrides consent, taint persists across turns, audit count matches outbound connections, prompt-injection documents cannot alter taint.

Phases 3 to 5 (retrieval, agent and permission layer, packaging and results) were built after review. See `PLAN.md` for status and `TEST_RESULTS.md` for what has and has not been verified.
