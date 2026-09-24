# Hermes Agent Integration

Exposes this project's actions (reminders, Gmail, Calendar) to the user's separately-installed
Hermes Agent (Nous Research, `~/.hermes`) as an MCP server, so Hermes's own self-improvement loop
drives the autonomy instead of bespoke cron/polling logic living in this project.

## Two explicit deviations (user's choice, against my recommendation)

1. **No staging for Hermes-triggered writes.** Every call this integration makes to
   `POST /v1/actions/{name}` sends `confirm: true` immediately. `gmail_send`/`gmail_reply`/
   `calendar_create` fire the moment Hermes decides to call them — no popup, no review window.
   They still land in `/v1/actions-audit` and the Logs tab afterward. This project's own chat UI
   keeps its existing staged-confirmation behavior untouched; only the Hermes path skips it.
2. **Full content exposure.** `gmail_read`/`calendar_read` results reach Hermes unmasked, exactly
   as they already reach this project's own local model today.

## What was found

- Hermes is already installed and running locally (`~/.hermes/config.yaml`). No `mcp_servers:`
  entry yet, but it natively supports them — each becomes an auto-loaded `mcp-<name>` toolset.
- **Hermes's own ESTOP is currently engaged** (`~/.hermes/ESTOP`). It won't act on anything,
  including this integration, until the user clears it.
- Hermes's `approvals` block (`cron_mode: deny`, `unattended_mode: deny`, `single_query_mode: deny`)
  governs all of its unattended behavior, not just this integration — not touched here.

## Design

`integrations/hermes_mcp/server.py`: a stdio MCP server that fetches `GET /v1/actions` from the
gateway at startup (so tool definitions never drift from `infra/config.yaml`), and proxies each
call to `POST /v1/actions/{name}` with `confirm: true`, returning the raw result. Auth via a
bearer token in its own env (`PRIVATE_AI_GATEWAY_TOKEN`), sourced from `~/.hermes/.env`.

## Setup

1. Put the gateway's owner token in `~/.hermes/.env` as `PRIVATE_AI_GATEWAY_TOKEN`.
2. Add to `~/.hermes/config.yaml`:
   ```yaml
   mcp_servers:
     private_ai:
       command: python3
       args: ["/absolute/path/to/private-ai/integrations/hermes_mcp/server.py"]
       env:
         PRIVATE_AI_GATEWAY_URL: "http://127.0.0.1:8080"
         PRIVATE_AI_GATEWAY_TOKEN: "${PRIVATE_AI_GATEWAY_TOKEN}"
   ```
3. Clear Hermes's ESTOP and restart it. Confirm the `mcp-private_ai` toolset appears in its tool
   list.

## What this feels like

Nothing new to learn — talk to Hermes as usual, on any surface (CLI, Telegram, etc.). It decides
which tool to call.

| You type to Hermes | What happens |
|---|---|
| "Remind me to call the dentist tomorrow morning" | `reminder_create` fires immediately. "Done." |
| "What's in my inbox from Sarah this week?" | `gmail_read` with a query, summarized in the reply. |
| "Reply to Sarah's email and tell her I'm in for Friday" | Reads the thread for the real message id, then `gmail_reply` — sends for real, no confirmation step. Repeat requests like this teach Hermes's self-improvement loop to write a skill for "look up the message id before replying." |
| "Schedule a 30 min sync with Alex next Tuesday at 3pm" | `calendar_create` fires immediately; the event is real. |
| "What's on my calendar today?" | `calendar_read`, summarized. |
| "Clear my old reminders about the conference" | `reminder_list` then `reminder_delete` on matches, no per-item confirmation. |
| "Keep an eye on that thread with trialbasis44 and reply once they respond" | Not covered by this integration alone — needs Hermes's own `cron_mode`/`unattended_mode` changed from `deny`, a separate decision. |

## Not done here

- Hermes-side `~/.hermes/config.yaml` and `~/.hermes/.env` edits are the user's own, not committed
  to this repo.
- Unattended/cron-triggered use (Hermes's `approvals` settings) — separate decision, not changed.
- Hermes's ESTOP — not cleared by this work; the user does that themselves.
