"""The built-in test UI: served with a strict content policy, text-only rendering, no outside
requests, and the read endpoints it uses only ever return the caller's own records."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from conftest import ROOT, auth, tool_defs, user_body

UI = ROOT / "gateway" / "ui"


def test_ui_is_served_with_a_strict_content_security_policy(make_rig):
    rig = make_rig()
    r = rig.client.get("/ui")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    for part in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
                 "img-src 'none'", "form-action 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert part in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "Private AI Agent" in r.text


def test_static_assets_are_served_and_nothing_else(make_rig):
    rig = make_rig()
    assert rig.client.get("/ui/app.js").headers["content-type"].startswith("text/javascript")
    assert rig.client.get("/ui/style.css").headers["content-type"].startswith("text/css")
    for bad in ("/ui/index.html", "/ui/../main.py", "/ui/..%2Fconfig.py", "/ui/%2e%2e/config.py", "/ui/secrets", "/ui/app.js/x"):
        assert rig.client.get(bad).status_code in (404, 405), bad
    assert rig.client.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_ui_can_be_switched_off_by_config(make_rig):
    import yaml
    from gateway.config import Settings
    from gateway.main import create_app
    from fastapi.testclient import TestClient
    raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
    raw["ui"]["enabled"] = False
    raw["rag"]["db_path"] = ":memory:"
    client = TestClient(create_app(Settings(**raw)))
    assert client.get("/ui").status_code == 404 and client.get("/ui/app.js").status_code == 404


def test_ui_source_makes_no_outside_requests_and_never_writes_html():
    html, js, css = (UI / "index.html").read_text(), (UI / "app.js").read_text(), (UI / "style.css").read_text()
    for name, text in (("index.html", html), ("app.js", js), ("style.css", css)):
        assert not re.search(r"(https?:)?//[A-Za-z0-9-]+\.[A-Za-z]{2,}", text), f"outside URL in {name}"
        assert "@import" not in text and "url(" not in text
    # every reply is shown as text; nothing from the server is ever parsed as HTML
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "srcdoc", "createContextualFragment", "DOMParser"):
        assert banned not in js, banned
    # inline script or handlers would be blocked by the policy anyway; make sure none are relied on
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son[a-z]+\s*=", html)
    assert " style=" not in html
    assert "textContent" in js


def test_hidden_attribute_always_wins_over_component_styles():
    """Regression: a display rule on a component once overrode `hidden` and showed controls that do not apply."""
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", (UI / "style.css").read_text())


def test_ui_javascript_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, "--check", str(UI / "app.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_ui_ids_used_by_the_script_exist_in_the_page():
    html, js = (UI / "index.html").read_text(), (UI / "app.js").read_text()
    ids = set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', js))
    ids |= {"tab-" + t for t in ("chat", "tools", "logs")}
    ids -= {"tab-"}
    missing = [i for i in ids if f'id="{i}"' not in html]
    assert not missing, missing


# ---- endpoints the UI reads --------------------------------------------------------------------------------------------

def test_audit_endpoint_returns_only_the_callers_records_without_prompt_text(make_rig):
    rig = make_rig(auto_route_clean=True)
    rig.chat("owner question about CANARY-UI-1", user="owner")
    rig.chat("guest question", user="guest")
    mine = rig.client.get("/v1/audit", headers=auth("owner")).json()["records"]
    assert [r["user"] for r in mine] == ["owner"] and mine[0]["lane"] == "frontier"
    assert "CANARY-UI-1" not in json.dumps(mine) and "question" not in json.dumps(mine)
    assert set(mine[0]) == {"ts", "lane", "user", "prompt_sha256", "tokens_in", "tokens_out",
                            "destination", "consent_mode", "status"}
    theirs = rig.client.get("/v1/audit", headers=auth("guest")).json()["records"]
    assert [r["user"] for r in theirs] == ["guest"]


def test_audit_endpoint_orders_newest_first_and_respects_limit(make_rig):
    rig = make_rig(auto_route_clean=True)
    for i in range(5):
        rig.chat(f"q{i}")
    rows = rig.client.get("/v1/audit?limit=3", headers=auth()).json()["records"]
    assert len(rows) == 3 and rows[0]["ts"] >= rows[-1]["ts"]
    assert len(rig.client.get("/v1/audit?limit=9999", headers=auth()).json()["records"]) == 5


def test_security_endpoint_returns_only_the_callers_events(make_rig):
    rig = make_rig()
    rig.tool("write_file", {"path": "x"}, user="owner")
    rig.tool("exec", {"cmd": "id"}, user="guest")
    mine = rig.client.get("/v1/security", headers=auth("owner")).json()["events"]
    assert [e["tool"] for e in mine] == ["write_file"] and all(e["user"] == "owner" for e in mine)
    assert "cmd" not in json.dumps(mine)


def test_log_endpoints_require_auth(make_rig):
    rig = make_rig()
    assert rig.client.get("/v1/audit").status_code == 401
    assert rig.client.get("/v1/security").status_code == 401
