"""Same as stub_server.py, but with a local reminder action enabled and a scripted two-step
model reply: propose a reminder_create tool call, then confirm once the result comes back.
Used only by natural_language_tools.mjs, kept separate so the two original UI scenarios (which
must stay decoupled from whatever a developer's local infra/config.yaml happens to have set) are
never affected by this file."""
import copy
import os
import pathlib
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ui-actions-e2e-"))
os.environ.update(GATEWAY_TOKEN_OWNER="owner-token-123", ANTHROPIC_API_KEY="stub-key",
                  RAG_DB_PATH=str(tmp / "rag.db"), GATEWAY_CONFIG=str(ROOT / "infra" / "config.yaml"))

import uvicorn                                   # noqa: E402
from gateway.config import Settings              # noqa: E402
from gateway.main import create_app              # noqa: E402
from stubs import FrontierStub, LocalStub, call  # noqa: E402

local, front = LocalStub(), FrontierStub()
# Step 1: the model, offered reminder_create, proposes calling it. Step 2 (after the client
# executes it and sends the result back): the model gives a plain confirmation.
local.script = [
    {"tool_calls": [call("reminder_create", {"text": "buy milk"}, "t1")]},
    {"content": "Done! I've created a reminder to buy milk."},
]
raw = copy.deepcopy(yaml.safe_load((ROOT / "infra" / "config.yaml").read_text()))
raw["model_server"]["base_url"] = local.url + "/v1"
raw["model_server"]["health_url"] = local.url + "/api/tags"
raw["frontier"].update(base_url=front.url, consent_scope="session", auto_route_clean=False)
raw["rag"].update(db_path=str(tmp / "rag.db"), embedder={"kind": "hashing", "dim": 2048})
raw["audit"]["path"] = str(tmp / "audit.jsonl")
raw["security"]["path"] = str(tmp / "sec.jsonl")
raw["tools"]["files_root"] = str(tmp / "files")
# Deliberately NOT inherited from disk: a developer's live infra/config.yaml may have every
# action enabled for their own real Gmail/Calendar testing, which must never leak into this test.
raw["actions"]["enabled"] = ["reminder_create", "reminder_list"]
raw["actions"]["reminders_dir"] = str(tmp / "actions" / "reminders")
raw["actions"]["audit_path"] = str(tmp / "actions-audit.jsonl")
(tmp / "files" / "owner").mkdir(parents=True)
print("READY", flush=True)
uvicorn.run(create_app(Settings(**raw)), host="127.0.0.1", port=int(os.environ.get("PORT", "8091")),
            log_level="warning", loop="asyncio")
