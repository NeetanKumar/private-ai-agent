# Known Gaps: Private AI Agent Template

Defects and limitations in what is already built. Each one was confirmed by hand. This is separate
from `FUTURE_SCOPE.md`, which covers work not yet started, and from `TEST_RESULTS.md`, which records
what has and has not been verified.

## Context and sessions

- **No context management.** History and retrieved chunks accumulate without trimming, counting or
  summarising. Every documents question adds up to five more chunks permanently.
- **The model's context window is not enforced.** The `context` value under `models.aliases` in
  `infra/config.yaml` is a label; it is neither sent to the model server nor checked. A small
  reasoning model can therefore exhaust the server's own default window while thinking and return an
  empty reply. Workarounds: set `models.extra_body: { reasoning_effort: none }`, or raise the model
  server's window.
- **One session per user.** Two browser tabs share one session, one history and one taint. Separate
  conversations would need session identifiers.
- **Sessions are held in memory.** A gateway restart clears every session and its taint. That fails
  safe, but it is not durable.

## Behaviour

- **Attached context is ignored in documents mode.** A request that both attaches context and sets
  `documents: true` is answered from retrieval alone; the attached text is stored and taints the
  session but never reaches the model.
- **The score floor cannot separate near-miss questions.** Of ten no-answer questions, the floor gates
  the six off-topic ones. The four whose topic is present but whose fact is absent depend on the local
  model obeying its instruction to reply "not in documents", which is unverified.

## Action tools (Gmail, Calendar, reminders)

- **No idempotency key on `POST /v1/actions/{name}`.** Repeating the same `confirm: true` call
  executes it again — a retried "send email" sends twice.
- **`judge_confirmation_needed()` is a wording heuristic, not a model call, and is a real
  guardrail exception.** It only inspects text for it: `context_hint` MUST be the user's own
  typed words, never text from a document/email, or an injection can talk its way past
  confirmation. See `gateway/action_tools.py`'s module docstring.
- **No injection-corpus coverage** of the confirmation heuristic specifically.
- **A message can't both reach the frontier and call a tool in the same turn.** Offering tools
  forces the private lane even if none gets called, so the chat UI withholds tools whenever a
  message is eligible to reach the frontier this turn (see `docs/AGENT_ACTIONS_PLAN.md`).
- **Gmail/Calendar executors are unit-tested against a mocked transport only** — never called
  against the real Google APIs (needs the one-time OAuth step, see `docs/AGENT_ACTIONS_PLAN.md`).

## Unverified

- **Retrieval quality with a real embedding model.** The measured recall and reciprocal rank come from
  an offline word-matching stand-in. The score floor is uncalibrated for any real embedder.
- **Faithfulness is not measured at all.** The harness and its live mode exist and their plumbing is
  tested; no model has been judged.
- **No model has been scored on tool use.** The twenty-task scorer works, but the only score so far is
  from a scripted stand-in.
- **The agent chassis has never been run.** Its configuration was written from documentation, part of
  which was unreachable, and two conflicting layouts are both present in the file.
- **Host-level protections are untested.** The outside port scan, the outbound firewall rules and the
  packet-capture reconciliation all need a real server.
- **On a Mac the outbound restriction does not apply,** because the model server runs outside Docker.
