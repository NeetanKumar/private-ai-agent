# Guardrails

- **Taint engine**: PRIVATE context can never reach the frontier, regardless of config/consent (deterministic code, not model judgment)
- **Two-lane routing**: local model default; frontier only for CLEAN + explicit consent
- **Tool allowlist**: 4 hardcoded read-only tools (`gateway/tools.py`), nothing else callable
- **Fail-closed**: local model down → error, never falls back to frontier
- **Audit log**: every frontier call logged (hashes/counts only, no prompt text)
- **Egress restriction**: only gateway container can reach the internet (host firewall + network isolation)
- **Auth**: bearer token per user, no anonymous access
- **Output sanitization**: strips auto-loading images/active HTML from replies (exfiltration via markdown)
- **Injection resistance**: tool/doc content treated as untrusted data, can't alter taint or trigger tools
- **`/new`**: only way to clear taint — no auto-decay, no heuristics
- **Action-tool allowlist**: 8 hardcoded actions (`gateway/action_tools.py`), empty by default, config can only narrow
- **Action confirmation**: write actions stage instead of executing unless confirmed; `gmail_send`/`gmail_reply`/`calendar_create` use a wording heuristic (not a model call) with a fail-safe "require confirmation" default — flagged exception, see `docs/AGENT_ACTIONS_PLAN.md`
- **Action audit log**: every staged/executed action logged (hash of args, never raw args)
- **Natural-language tool/action calling**: chat UI offers both registries to the model on ordinary messages; calls are validated against their own registry only (never cross-classified), and offering tools forces the private lane even if none is called — the UI withholds tools on any message eligible for the frontier this turn, so a message can't both reach the frontier and call a tool in the same turn
