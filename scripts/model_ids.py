"""Print the concrete model ids from infra/config.yaml, one per line (used by `make pull-models`)."""
import sys
import yaml

cfg = yaml.safe_load(open(sys.argv[1] if len(sys.argv) > 1 else "infra/config.yaml"))
for m in cfg["models"]["aliases"].values():
    print(m["id"])
