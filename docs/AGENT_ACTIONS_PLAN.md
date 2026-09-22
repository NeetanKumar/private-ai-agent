# Agent Actions Plan (Gmail, Calendar, Reminders)

**Implemented**, except the one step that needs your live browser (Google OAuth consent).
Code: `gateway/action_tools.py`, `gateway/config.py` (`ActionsCfg`), `gateway/main.py`
(`/v1/actions`, `/v1/actions/{name}`, `/v1/actions-audit`). Tests: `tests/test_action_tools.py`,
`tests/test_action_endpoints.py`, `tests/security/test_action_permissions.py`.

**To finish**: `python scripts/google_oauth_setup.py --client-id ... --client-secret ...`
(needs a Google Cloud OAuth client of type "Desktop app" with Gmail + Calendar APIs enabled),
paste the three printed values into `infra/.env`, then list the tools you want under
`actions.enabled` in `infra/config.yaml` and `make up`.

## ⚠️ Two explicit deviations from this project's core design (your choices, flagged)

1. **Execution mode for `gmail_send` / `gmail_reply` / `calendar_create` uses a wording
   heuristic** (`judge_confirmation_needed()` in `gateway/action_tools.py`), not deterministic
   per-action confirmation. It is a plain regex match, not a model call, so it is testable and
   has a fail-safe default of requiring confirmation. But it only looks at wording: text
   containing an explicit-consent phrase skips confirmation regardless of who wrote it. Callers
   MUST only pass the user's own typed words as `context_hint`, never text drawn from a document
   or email body — the function has no way to tell the difference. This is proven directly in
   `tests/test_action_tools.py::test_documented_limit_the_heuristic_cannot_tell_injected_text_from_real_consent`.
   Real risk, accepted per your instruction.

2. **Gmail/Calendar content taint is left at the project's own default (PRIVATE)**, not made
   CLEAN. The shipped `infra/config.yaml` registers no `tool:gmail_*` / `tool:calendar_*`
   source as CLEAN. You asked for content-based CLEAN judgment; the deterministic taint engine
   has no such mechanism (nor should it gain one — see `gateway/taint.py`'s own docstring: "no
   model or classifier is ever consulted"). The only lever that exists is the same source
   registry every other tool uses, and marking Gmail/Calendar sources CLEAN there is a real,
   available, but NOT-taken-by-default choice — flagged in `infra/config.yaml`'s `actions:`
   block and guarded by `tests/security/test_action_permissions.py::test_shipped_default_has_no_source_registered_clean_for_google_content`.
   If you add that registration yourself, re-read the risk noted in `gateway/action_tools.py`'s
   module docstring first.

## What was built

- `ACTION_TOOLS` (`gateway/action_tools.py`): `reminder_create/list/delete` (local, no OAuth),
  `gmail_read/send/reply`, `calendar_read/create`. Same "hardcoded in code, config can only
  narrow" rule as `gateway/tools.py`'s `READ_ONLY_TOOLS` (`gateway/config.py: ActionsCfg`
  validates `actions.enabled` against it, unknown name = startup error).
- Google access is plain REST over `httpx` (already a dependency) — no `google-*` SDK was
  added, so no new network-calling dependency needed approval.
- Invocation mirrors the existing read-only tool pattern: `GET /v1/actions` lists definitions,
  `POST /v1/actions/{name}` executes or stages one call. The gateway does not run a
  tool-calling loop for these any more than it does for read-only tools.
- Confirmation: `POST /v1/actions/{name}` with `confirm` omitted stages (nothing executes) if
  the tool's `requires_confirmation` is `True`, or `"judge"` and the heuristic says so. A
  second call with `confirm: true` executes it.
- Every staged and executed call is written to `ActionAuditLog` (`gateway/audit.py`,
  `/audit/actions.jsonl`) with a hash of the arguments, never the raw arguments, mirroring the
  existing egress `AuditLog`.
- Storage: reminders live in one JSON file per user under `actions.reminders_dir`, on its own
  `actions_data` Docker volume (writable; the container filesystem is otherwise read-only).

## Known gap (also in docs/KNOWN_GAPS.md)

`POST /v1/actions/{name}` has no idempotency key. Repeating the same `confirm: true` request
executes the action again — a retried "send email" call sends twice. Not built tonight; a
client-supplied idempotency key checked against `ActionAuditLog` would close it.

## Not done

- No UI for actions (the UI was recently trimmed to Chat + Logs only; these are API-only,
  matching how read-only tools work without a UI tab).
- No injection-corpus extension exercising the confirmation heuristic against hostile document
  text specifically (the existing 12-document corpus in `tests/security/injection_corpus/`
  targets the read-only tools and lane/taint rules, not this new endpoint).
- Calendar/Gmail executors are unit-tested against a mocked `httpx` transport only; nothing has
  called the real Google APIs, because that needs the OAuth step above.
