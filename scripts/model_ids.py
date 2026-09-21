"""Print every model id the stack needs, one per line, from infra/config.yaml.
Used by `make models` / `make models-mac`: the chat models plus the embedding model when Ollama serves it."""
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "infra/config.yaml"))
ids = [m["id"] for m in cfg["models"]["aliases"].values()]
emb = (cfg.get("rag") or {}).get("embedder") or {}
if cfg.get("rag", {}).get("enabled", True) and emb.get("kind") == "ollama" and emb.get("model"):
    ids.append(emb["model"])
print("\n".join(dict.fromkeys(ids)))
