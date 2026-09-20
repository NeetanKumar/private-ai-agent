COMPOSE = docker compose -f infra/docker-compose.yml --env-file infra/.env
PY ?= python3

.PHONY: up up-mac down logs pull-models pull-models-mac test venv

up:            ## rented GPU host: gateway + Ollama container
	$(COMPOSE) --profile gpu up -d --build

up-mac:        ## dev laptop: gateway only, Ollama runs natively on the host
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) --profile gpu down

logs:
	$(COMPOSE) --profile gpu logs -f --tail=100

pull-models:   ## pull every model listed in infra/config.yaml into the Ollama container
	@for m in $$($(PY) scripts/model_ids.py); do $(COMPOSE) --profile gpu exec ollama ollama pull $$m; done

pull-models-mac:
	@for m in $$($(PY) scripts/model_ids.py); do ollama pull $$m; done

venv:
	$(PY) -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test:
	.venv/bin/python -m pytest tests -q
