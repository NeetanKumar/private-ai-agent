# Private AI Agent Template. Run `make help`.
COMPOSE = docker compose -f infra/docker-compose.yml --env-file infra/.env
PY     ?= python3
VENV    = .venv

.DEFAULT_GOAL := help
.PHONY: help init up up-mac down logs models models-mac pull-models pull-models-mac venv test scrub ingest docs-list

help:          ## show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

init:          ## create infra/.env with a generated gateway token (never overwrites)
	@$(PY) scripts/init_env.py

up: init       ## GPU host: gateway + Ollama container (then run `make models` once)
	$(COMPOSE) --profile gpu up -d --build

up-mac: init   ## laptop: gateway only; Ollama runs natively on the host
	OLLAMA_BASE_URL=$${OLLAMA_BASE_URL:-http://host.docker.internal:11434} $(COMPOSE) up -d --build

down:          ## stop everything (data volumes are kept)
	$(COMPOSE) --profile gpu down

logs:          ## follow container logs (they never contain prompt bodies)
	$(COMPOSE) --profile gpu logs -f --tail=100

# The model list is read inside the gateway image, which always has PyYAML (the host may not).
MODEL_IDS = $(COMPOSE) run --rm --no-deps -T gateway python /app/scripts/model_ids.py /app/infra/config.yaml 2>/dev/null

models: init   ## pull the chat models and the embedding model named in infra/config.yaml into the Ollama container
	@for m in $$($(MODEL_IDS)); do $(COMPOSE) --profile gpu exec ollama ollama pull $$m || exit 1; done

models-mac: init ## pull the same models into a native Ollama on this machine
	@for m in $$($(MODEL_IDS)); do ollama pull $$m || exit 1; done

pull-models: models
pull-models-mac: models-mac

venv:          ## create .venv with the test dependencies
	$(PY) -m venv $(VENV) && $(VENV)/bin/pip install -q -r requirements-dev.txt

test: venv     ## run the full harness and regenerate docs/TEST_RESULTS.md
	$(VENV)/bin/python scripts/run_harness.py

scrub:         ## check tracked files for personal names: SCRUB_TERMS="name1,name2" make scrub
	@SCRUB_TERMS="$(SCRUB_TERMS)" $(VENV)/bin/python -m pytest tests/test_repo_hygiene.py -q -k scrub

# usage: make ingest USER_ID=owner SOURCE=my_notes FILES="/inbox/owner/a.md /inbox/owner/b.pdf"
ingest:        ## ingest files from data/inbox into a user's private document store
	$(COMPOSE) run --rm --no-deps gateway python -m rag.ingest --user $(USER_ID) --source $(SOURCE) $(FILES)

docs-list:     ## list a user's ingested documents: make docs-list USER_ID=owner
	$(COMPOSE) run --rm --no-deps gateway python -m rag.ingest --user $(USER_ID) --list
