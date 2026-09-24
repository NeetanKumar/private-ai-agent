# Hermes MCP adapter

Exposes this project's action tools (reminders, Gmail, Calendar) to Hermes Agent as an MCP server.
See `docs/HERMES_INTEGRATION.md` at the repo root for the full design and the two explicit
guardrail deviations this integration makes (no staging for writes, full content exposure).

No `mcp` SDK dependency - a small hand-rolled JSON-RPC-over-stdio server (see `server.py`'s
docstring for why). Only dependency is `httpx`, already used elsewhere in this project.

## Setup

1. Make sure the gateway is running (`make up-mac` from the repo root) and note its owner token
   (`GATEWAY_TOKEN_OWNER` in `infra/.env`).
2. Put that token in `~/.hermes/.env`:
   ```
   PRIVATE_AI_GATEWAY_TOKEN=<the token>
   ```
3. Add to `~/.hermes/config.yaml`:
   ```yaml
   mcp_servers:
     private_ai:
       command: python3
       args: ["/absolute/path/to/private-ai/integrations/hermes_mcp/server.py"]
       env:
         PRIVATE_AI_GATEWAY_URL: "http://127.0.0.1:8080"
         PRIVATE_AI_GATEWAY_TOKEN: "${PRIVATE_AI_GATEWAY_TOKEN}"
   ```
   Replace the path with this repo's actual location on your machine.
4. Clear Hermes's ESTOP if engaged, and restart Hermes. Confirm the `mcp-private_ai` toolset
   appears in its tool list.

## Verify without Hermes

```
export PRIVATE_AI_GATEWAY_URL=http://127.0.0.1:8080
export PRIVATE_AI_GATEWAY_TOKEN=<the token>
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 server.py
```
Should print one JSON line listing every action enabled in `infra/config.yaml`'s `actions.enabled`.

## Behavior notes

- Every tool call is executed immediately (`confirm: true`); nothing is staged. Writes
  (`gmail_send`, `gmail_reply`, `calendar_create`) fire the moment Hermes calls them.
- Every call still appears in the gateway's `/v1/actions-audit` and Logs tab, whether it came from
  Hermes or the project's own UI.
- Tool definitions are fetched from the gateway (`GET /v1/actions`) once at server startup, so they
  can never drift from `infra/config.yaml`. Restart this server after changing `actions.enabled`.
