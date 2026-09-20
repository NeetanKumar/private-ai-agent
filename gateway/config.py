"""Config loader. Models and endpoints come from infra/config.yaml, never from code."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict

import yaml
from pydantic import BaseModel


class GatewayCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class ModelServerCfg(BaseModel):
    base_url: str
    health_url: str
    timeout_seconds: float = 300


class ModelCfg(BaseModel):
    id: str
    context: int = 0


class ModelsCfg(BaseModel):
    default: str
    aliases: Dict[str, ModelCfg]


class Settings(BaseModel):
    gateway: GatewayCfg = GatewayCfg()
    model_server: ModelServerCfg
    models: ModelsCfg

    def resolve_model(self, requested: str | None) -> str:
        """Map an alias (or the empty value) to a concrete model id. Unknown ids pass through
        only if they are a configured model id; anything else is rejected by the caller."""
        name = requested or self.models.default
        if name in self.models.aliases:
            return self.models.aliases[name].id
        if name in {m.id for m in self.models.aliases.values()}:
            return name
        raise KeyError(name)


def _apply_env(raw: dict) -> dict:
    base = os.environ.get("OLLAMA_BASE_URL")
    if base:
        base = base.rstrip("/")
        raw["model_server"]["base_url"] = base + "/v1"
        raw["model_server"]["health_url"] = base + "/api/tags"
    return raw


@lru_cache
def get_settings() -> Settings:
    path = Path(os.environ.get("GATEWAY_CONFIG", "infra/config.yaml"))
    return Settings(**_apply_env(yaml.safe_load(path.read_text())))
