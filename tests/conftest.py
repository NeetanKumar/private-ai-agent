import copy
import os
import pathlib
import tempfile

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ["GATEWAY_CONFIG"] = str(ROOT / "infra" / "config.yaml")
os.environ.setdefault("GATEWAY_TOKEN_OWNER", "test-owner-token")
os.environ.setdefault("RAG_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="rag-test-"), "rag.db"))

from gateway.config import Settings  # noqa: E402
from gateway.main import create_app  # noqa: E402
from stubs import FrontierStub, LocalStub  # noqa: E402
from stubs import call  # noqa: E402,F401

TOKENS = {"owner": "test-owner-token", "guest": "test-guest-token"}


def auth(user: str = "owner") -> dict:
    return {"Authorization": f"Bearer {TOKENS[user]}"}


def tool_defs(*names) -> list:
    """Client-side tool definitions, as an agent would send them. The gateway only reads the names."""
    return [{"type": "function", "function": {"name": n, "description": "client-supplied", "parameters": {}}}
            for n in names]


def user_body(text: str, **extra) -> dict:
    return {"messages": [{"role": "user", "content": text}], **extra}


class Rig:
    def __init__(self, client, local, frontier, audit_path, files_root=None, sec_path=None):
        self.client, self.local, self.frontier, self.audit_path = client, local, frontier, audit_path
        self.files_root, self.sec_path = files_root, sec_path

    def put_file(self, user, rel, text):
        p = self.files_root / user / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def sec_events(self):
        import json as _j
        return [_j.loads(l) for l in self.sec_path.read_text().splitlines() if l] if self.sec_path.exists() else []

    def tool(self, name, args, user="owner"):
        return self.client.post(f"/v1/tools/{name}", json={"arguments": args}, headers=auth(user))

    def post(self, body, user="owner"):
        return self.client.post("/v1/chat/completions", json=body, headers=auth(user))

    def chat(self, text, user="owner", **extra):
        return self.client.post("/v1/chat/completions", json=user_body(text, **extra), headers=auth(user))

    def session(self, user="owner"):
        return self.client.get("/session", headers=auth(user)).json()

    def audit_lines(self):
        if not self.audit_path.exists():
            return []
        return [l for l in self.audit_path.read_text().splitlines() if l]


@pytest.fixture
def make_rig(tmp_path, monkeypatch):
    """Build a gateway wired to real stub servers. Adding the second user is config only."""
    made = []

    def _make(auto_route_clean=False, consent_scope="session", extra_sources=None):
        monkeypatch.setenv("GATEWAY_TOKEN_OWNER", TOKENS["owner"])
        monkeypatch.setenv("GATEWAY_TOKEN_GUEST", TOKENS["guest"])
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        local, frontier = LocalStub(), FrontierStub()
        raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
        raw = copy.deepcopy(raw)
        raw["model_server"]["base_url"] = local.url + "/v1"
        raw["model_server"]["health_url"] = local.url + "/api/tags"
        raw["frontier"].update(auto_route_clean=auto_route_clean, consent_scope=consent_scope,
                               base_url=frontier.url, timeout_seconds=5)
        raw["users"].append({"id": "guest", "token_env": "GATEWAY_TOKEN_GUEST"})
        raw["sources"].update({"public_docs": "CLEAN", "crm": "PRIVATE", **(extra_sources or {})})
        raw["rag"].update(db_path=str(tmp_path / f"rag-{len(made)}.db"), min_score=0.10,
                          embedder={"kind": "hashing", "dim": 2048})
        audit_path = tmp_path / f"audit-{len(made)}.jsonl"
        raw["audit"]["path"] = str(audit_path)
        files_root = tmp_path / f"files-{len(made)}"
        sec_path = tmp_path / f"security-{len(made)}.jsonl"
        raw["tools"]["files_root"] = str(files_root)
        raw["security"]["path"] = str(sec_path)
        app = create_app(Settings(**raw))
        client = TestClient(app)
        client.__enter__()
        made.append((client, local, frontier))
        return Rig(client, local, frontier, audit_path, files_root, sec_path)

    yield _make
    for client, local, frontier in made:
        client.__exit__(None, None, None)
        local.stop()
        frontier.stop()
