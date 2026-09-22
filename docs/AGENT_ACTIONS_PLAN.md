# Agent Actions Plan (Gmail, Calendar, Reminders)

Not started. Plan only.

## ⚠️ Two explicit deviations from this project's core design (your choices, flagged)

1. **Execution mode = model-judged ("tone"), not deterministic.**
   Every other decision in this project (routing, taint) is fixed code, never model judgment —
   on purpose, because a model can be talked out of a rule. Letting the model decide
   auto-send vs. ask-first from inferred tone means a cleverly-worded injected email could
   read as "confident" and trigger a real send/delete with no human step. Real risk, accepted
   per your instruction.

2. **Taint = content-based ("CLEAN if it looks clean"), not source-based PRIVATE-always.**
   A spoofed/injected email that *looks* clean could (a) trigger an action with no consent
   gate, and (b) under existing rules, CLEAN content is eligible to route to the frontier —
   meaning email content could leave to the Claude API without extra flagging. Real risk,
   accepted per your instruction.

Recommend revisiting both once real usage shows how often this bites.

## Scope
- Local reminders (no OAuth, no external egress)
- Gmail: read, send, reply
- Google Calendar: read, create events
- Existing 4 read-only tools (`gateway/tools.py`) untouched

## Architecture
- New tool category `ACTION_TOOLS`, separate dict from `READ_ONLY_TOOLS`, own allowlist,
  same "hardcoded in code, config can only narrow" pattern
- New audit trail for actions (timestamp, tool, user, args-hash, executed y/n) — separate
  from the frontier egress audit
- Google OAuth: refresh token in `infra/.env`, minimal scopes (`gmail.send`, `gmail.readonly`,
  `calendar.events`), never logged

## Phases
1. Local reminders — CRUD, local-lane only, lowest risk, no OAuth
2. Gmail read (list/read) — OAuth added, reuses read-only pattern
3. Gmail send/reply — action tool, tone-judged execution, audit per send
4. Calendar read
5. Calendar create — action tool

## Open risk to test explicitly
Phase 3+ needs an injection-corpus extension: hostile email bodies phrased to *sound* confident/
authorized. Given the chosen execution mode, this can't be guaranteed safe — only measured.
