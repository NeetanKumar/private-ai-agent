# Private AI Agent Template: Concise Plan

Short version of `PLAN.md`. Status: **Phases 0 to 5 built. 230 automated tests pass.** Everything that needs a real GPU host is still unverified (see the last section).

## Goal
A self-hosted, privacy-first agent. Private data goes to a local model only. A second lane calls Claude, but only for provably clean context, and by default only after consent.

## Hard rules
- Nothing public. Access by SSH tunnel or Tailscale. Only the gateway can go out, and only to the frontier host.
- **Routing = two checks, in order.** 1) Taint: PRIVATE never reaches the frontier, whatever the consent, flags or config. 2) Consent: per `auto_route_clean` (ships `false`) and `consent_scope` (`session` or `request`). No model or classifier ever routes.
- Every response shows its lane. If the local model fails, the private lane errors. It never falls back to the frontier.
- Tools are read-only, enforced in the gateway. Every egress is audit-logged (time, lane, user, prompt hash, token counts, destination, consent mode). No prompt bodies or user data in logs.
- One isolated session per user. Adding a user is a config change. Models are swappable by config.

| Context taint | `auto_route_clean` | Result |
|---|---|---|
| CLEAN | false | Local answer, frontier offered; egress only on consent |
| CLEAN | true | Frontier automatically, audit record written |
| PRIVATE | either | Local only. No offer, no egress. |

Taint is cleared only by `/new`. Unregistered sources are PRIVATE.

## Stack and models
- **Chassis:** OpenClaw (we write no agent loop). **Gateway:** FastAPI. **Serving:** Ollama, OpenAI-compatible. **Packaging:** Docker Compose. **Host:** rented dedicated 24 GB GPU.
- **Daily driver:** Qwen3.5-9B. **On-demand:** Qwen3.6-27B. Both Apache 2.0, 256K context, native tool calling. **Fallback:** Hermes 4 14B.
- Rejected, with reasons in `ONEPAGER.md`: Muse Glimmer 30B (too new, revisit), Gemma 4, gpt-oss-20b, Hermes 4.3 36B, the MoEs, Llama 4, Mistral Small 4, DeepSeek and Kimi.

## Phases
| # | What was built | Acceptance | Status |
|---|---|---|---|
| 0 | Model choice and one-pager | Two models chosen, rejects explained, stale items listed | Done |
| 1 | Compose stack, gateway proxy, health, loopback-only port | Completion through the gateway; outside scan shows nothing | Built; scan pending host |
| 2 | Taint engine, two lanes, single egress function, audit log, `/new` | 200-query canary run, taint beats consent, history propagation, both `auto_route_clean` values, audit count = connection count, fail closed, injection cannot change taint | Built and tested with stand-in servers; packet capture pending host |
| 3 | Ingest (md, txt, pdf), chunking, per-user store behind an interface, retrieval with taint inheritance, private lane only | 50 Q/A, 10 no-answer, recall@5, MRR, faithfulness, user isolation | Built; retrieval measured offline only; faithfulness not measured |
| 4 | Read-only tools, permission layer, tainted tool results, sanitised replies, injection corpus, 20-task scorer, OpenClaw config and tool bridge | Writes blocked at gateway; injection cannot change lane, taint or exfiltrate; 20-task score | Built; OpenClaw never run; no real-model score |
| 5 | `make init/up/down/models/test`, README, RUNBOOK, generated `TEST_RESULTS.md`, hygiene checks | Full harness runs; no names or secrets in files | Done |

## Measured so far (stand-in servers, offline embedder)
- Tests: 230 pass, 0 fail.
- Retrieval: recall@5 0.92, MRR 0.84. The no-answer score floor catches 6 of 10 questions and wrongly rejects 5 of 50 answerable ones.
- Injection corpus: 12 hostile documents through 4 channels, against a model that obeys them. Lane, taint, egress and tool policy held every time.
- 20-task scorer: a scripted stand-in gets 20/20. This proves the harness only.

## Still needs a real GPU host
- Outside port scan, packet capture vs audit log, host firewall rules.
- A real model answering, and killing the real model container.
- Real embedder: retrieval quality and score-floor calibration. Faithfulness.
- The 4 near-miss "not in documents" questions (only 6 of 10 verified). Tool-use score of the real model.
- A live frontier call. Running OpenClaw with the provided config.

## Re-check before committing to a host
Qwen3.8-27B support, Muse Glimmer tool parser, Qwen tool-call reliability under Ollama, Hermes 4 14B context, licence texts, GPU prices, OpenClaw config schema (docs were unreachable), the embedding model tag, Claude model ids.

## Where to look
`README.md` overview and routing table. `RUNBOOK.md` deploy and operate. `TEST_RESULTS.md` every check and its result. `ONEPAGER.md` model reasoning. `PLAN.md` full detail.
