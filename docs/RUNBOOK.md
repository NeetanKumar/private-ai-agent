# Runbook: Private AI Agent Template

How to deploy, verify, operate and tear down the template on a rented GPU host. Steps that only a
real host can confirm are marked **[host]**. They are not covered by the automated tests.

## 1. Overview

```
your laptop --(Tailscale or SSH tunnel)--> GPU host
                                             gateway   127.0.0.1:8080   the only service with outbound access
                                             ollama    internal network only, no published port
```

Nothing listens on a public interface. The gateway is the only container that can reach the internet,
and the host firewall limits it to the frontier API host.

## 2. Choose and provision the host

- One dedicated 24 GB GPU (RTX 4090, L4 or A10G class) is enough for both models, one loaded at a
  time. Use a 48 GB card if you want both resident.
- Use a dedicated-tenancy tier with an encrypted disk. Do not use peer-to-peer marketplaces or spot
  hosts you cannot vet. Your private documents will live on this machine.
- Ubuntu 22.04 or 24.04 with the NVIDIA driver installed. Check with `nvidia-smi`.
- Prices and offerings change. Check them when you buy.

## 3. Harden the host **[host]**

```bash
# Tailscale: the host is reachable only over your tailnet
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Firewall: deny everything inbound except SSH over the tailnet
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0 to any port 22 proto tcp
sudo ufw enable
```

If the provider forces a public SSH port, restrict it to your own IP and disable password logins.

Install Docker and the NVIDIA container toolkit by following their current instructions, then check
that a container can see the GPU:

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

## 4. Deploy

```bash
git clone <this repository> private-ai && cd private-ai
make init                      # creates infra/.env (mode 600) with a random token for user "owner"
nano infra/.env                # optional: ANTHROPIC_API_KEY for the frontier lane
make up                        # builds and starts the gateway and the Ollama container
make models                    # pulls the chat models and the embedding model listed in infra/config.yaml
```

Leave `ANTHROPIC_API_KEY` empty to keep the frontier lane disabled. Requests that would use it then
fail with `frontier_not_configured` and make no connection.

Never commit `infra/.env`. It is git-ignored, and `make test` checks that no secret is tracked.

## 5. Restrict outbound traffic **[host]**

Only the gateway may reach the frontier API. Ollama and every other container sit on an internal
network with no route out. The firewall script limits the gateway's own network to the frontier
host on port 443 and drops everything else:

```bash
sudo infra/egress-allowlist.sh
```

The API host's IP addresses can change, so run it again every few minutes:

```bash
echo '*/5 * * * * root /path/to/private-ai/infra/egress-allowlist.sh' | sudo tee /etc/cron.d/private-ai-egress
```

If the host name cannot be resolved the script refuses to change the rules.

## 6. Verify before you put real data on it

**6.1 The gateway answers.** Easiest is the built-in UI: open an SSH tunnel and browse to `http://localhost:8080/ui`. The commands below do the same from a terminal.

Over an SSH tunnel from your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 <host>          # leave running
TOKEN=<value of GATEWAY_TOKEN_OWNER from infra/.env on the host>
curl -s localhost:8080/health
curl -s localhost:8080/v1/chat/completions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"messages":[{"role":"user","content":"Say hello"}]}'
```

Expect `"lane":"private"` in the reply.

**6.2 Nothing is exposed** **[host]**. From a different machine, not the host and not on the tailnet:

```bash
tests/test_portscan.sh <the host's public IP>
```

It must report that neither 8080 nor 11434 is reachable. Only SSH may appear, and only if the
provider requires it.

**6.3 Outbound traffic matches the audit log** **[host]**. On the host, as root, while you send a
mix of clean and private queries through the gateway:

```bash
sudo tests/privacy/tcpdump_check.sh /var/lib/docker/volumes/private-ai_audit_log/_data/audit.jsonl 120
```

It counts new outbound connections from the gateway network and compares them with the audit
records written in the same window. It fails on any connection to another destination.

**6.4 A private request never reaches the frontier.** Send a request with private context, with
`auto_route_clean` on and consent given, and confirm the audit log did not grow.

**6.5 Fail closed.** Stop Ollama (`docker compose -f infra/docker-compose.yml stop ollama`), send a
request, and confirm you get a 503 from the private lane and no audit record. Start it again after.

**6.6 Model and retrieval quality** **[host]**. These need the real models:

```bash
python tests/agent/run_eval.py --url http://localhost:11434/v1 --model <daily model id>   # tool-use score
python tests/rag/report.py --live http://localhost:8080 $TOKEN                             # answers and faithfulness
python tests/rag/report.py                                                                 # floor sweep (offline embedder)
```

Ingest the sample corpus first, using the real embedding model. Then run the floor sweep against the
real embedder and set `rag.min_score` from it. The shipped value is a placeholder. Record the results
in `docs/TEST_RESULTS.md`.

## 7. Everyday operation

| Task | How |
|---|---|
| Ask a question | The UI at `/ui`, or `POST /v1/chat/completions` with your bearer token. |
| Clear a session and its taint | Send exactly `/new`, or `POST /session/new`. It is the only way. |
| Allow the frontier for a session | `POST /session/consent {"consent": true}`. Cleared by `/new`. |
| Switch to per-request consent | Set `frontier.consent_scope: request` in `infra/config.yaml`, then `make up`. |
| Route clean context to the frontier automatically | Set `frontier.auto_route_clean: true`, then `make up`. Private context is still never sent. |
| Add a user | Add an entry under `users:` in `infra/config.yaml`, add `GATEWAY_TOKEN_<NAME>=<random>` to `infra/.env` using the `token_env` name you chose, then `make up`. |
| Rotate a token | Change its value in `infra/.env`, then `make up`. |
| Add documents | Put files in `data/inbox/<user id>/`, then `make ingest USER_ID=... SOURCE=... FILES="/inbox/<user id>/a.md"`. |
| List documents | `make docs-list USER_ID=...` |
| Mark a source CLEAN | Add it under `sources:` in `infra/config.yaml`. Only do this for data that is genuinely fine to send out. Anything not listed is PRIVATE. |
| Swap a model | Change the id under `models.aliases` in `infra/config.yaml`, run `make models`, then `make up`. If the new model uses a different tool-call format, re-run the tool-use score. |
| See your own audit and security records | The Logs tab in the UI, or `GET /v1/audit` and `GET /v1/security`. |
| Read the egress audit log | `docker compose -f infra/docker-compose.yml --env-file infra/.env exec gateway cat /audit/audit.jsonl` |
| Read the security log | Same, with `/audit/security.jsonl`. It lists blocked tool calls by name and reason. |
| Stop | `make down`. Data volumes are kept. |

Config changes take effect when the gateway restarts. `make up` recreates the container.

Sessions live in memory. Restarting the gateway clears all sessions and their taint together.

The gateway never logs prompt bodies or user data. The audit log holds a hash of each frontier
prompt, token counts, the destination, and the consent mode.

## 8. Backups

The document store is the `private-ai_rag_data` volume. The audit and security logs are in the
`private-ai_audit_log` volume. Model files in `private-ai_ollama_models` can be pulled again. Back up
the first two to somewhere you control, encrypted.

## 9. Tear down and wipe

```bash
make down
docker volume rm private-ai_rag_data private-ai_audit_log private-ai_ollama_models
```

Then destroy the instance and its disk through the provider. Do not just stop it, because your
documents are on that disk. Revoke the frontier API key if the host is retired.

## 10. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `401 unauthorized` | Wrong or missing bearer token, or the user's token variable is not set in `infra/.env`. |
| `503 local_model_unavailable` | Ollama is down or still loading. Check `make logs`. By design there is no fallback. |
| `503 retrieval_unavailable` | The embedding model is not pulled or Ollama is down. |
| `403 taint_blocked` | The session holds private context. Send `/new` if you really want a clean start. |
| `403 consent_required` | The frontier needs consent. See the table above. |
| `503 frontier_not_configured` | `ANTHROPIC_API_KEY` is empty in `infra/.env`. |
| `502 frontier_unavailable` | The frontier call failed. The attempt is still in the audit log. Check the firewall script. |
| Reply is always `not in documents` | The score floor is too high for your embedder. Run the floor sweep and lower `rag.min_score`. |
| A tool call is missing from a reply | It was outside the read-only allowlist or had bad arguments. See the security log. |
| Gateway will not start | Config error, often a tool name outside the read-only allowlist or a taint other than CLEAN or PRIVATE. Read `make logs`. |

## 11. Before real data goes on it

- [ ] Host is dedicated, with an encrypted disk, reachable only over Tailscale or an SSH tunnel.
- [ ] `tests/test_portscan.sh` from an outside machine shows nothing open.
- [ ] `infra/egress-allowlist.sh` is installed and scheduled, and `tcpdump_check.sh` passes.
- [ ] A private request produced no audit record. Stopping Ollama produced a 503 and no fallback.
- [ ] `rag.min_score` was set from the floor sweep with the real embedder.
- [ ] The tool-use score for the chosen model was recorded and is acceptable.
- [ ] `infra/.env` is mode 600 and not committed.
- [ ] You know how to wipe the host (section 9).
