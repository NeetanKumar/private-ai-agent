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
