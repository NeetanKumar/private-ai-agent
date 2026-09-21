"""Repository hygiene: secrets never committed, docs carry no personal references, the stack cannot be
published on a public address by default, and the README states the routing rules exactly."""
import os
import pathlib
import re
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

ROUTING_TABLE = """| Context taint | `auto_route_clean` | Result |
|---|---|---|
| CLEAN   | false | Local answer, frontier offered — egress only on explicit consent |
| CLEAN   | true  | Routes to frontier automatically, audit record written |
| PRIVATE | false | Local only. No offer, no egress. |
| PRIVATE | true  | Local only. No offer, no egress. |"""


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / p for p in out.splitlines() if (ROOT / p).is_file()]


def text_files():
    for p in tracked():
        try:
            yield p, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def docs_files():
    return [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))


# ---- secrets ------------------------------------------------------------------------------------------------------------

def test_no_env_file_is_tracked_and_it_is_ignored():
    names = [p.name for p in tracked()]
    assert ".env" not in names and not any(n.endswith((".pem", ".key")) for n in names)
    assert re.search(r"^\.env$", (ROOT / ".gitignore").read_text(), re.M)


def test_env_example_holds_no_secret_values():
    for line in (ROOT / "infra" / ".env.example").read_text().splitlines():
        if line.startswith(("GATEWAY_TOKEN_", "ANTHROPIC_API_KEY")):
            assert line.split("=", 1)[1].strip() == "", line


SECRET_PATTERNS = [r"sk-ant-[A-Za-z0-9_-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"AKIA[0-9A-Z]{16}",
                   r"gh[pousr]_[A-Za-z0-9]{20,}", r"xox[baprs]-[A-Za-z0-9-]{10,}"]


def test_no_secret_shaped_strings_in_tracked_files():
    for p, text in text_files():
        for pat in SECRET_PATTERNS:
            assert not re.search(pat, text), f"{p.relative_to(ROOT)} matches {pat}"


# ---- no personal references -----------------------------------------------------------------------------------------

def test_docs_and_readme_carry_no_emails_home_paths_or_account_urls():
    for p in docs_files():
        text = p.read_text()
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text), f"email-like text in {p.name}"
        assert "/Users/" not in text and "/home/" not in text, f"home path in {p.name}"
        assert "github.com/" not in text, f"account or repository URL in {p.name}"


def test_readme_names_the_project_only_as_the_template():
    assert (ROOT / "README.md").read_text().startswith("# Private AI Agent Template")


def test_no_tracked_file_contains_an_email_or_home_path():
    allowed = re.compile(r"(noreply|example\.(com|org|internal))")
    for p, text in text_files():
        if p.suffix == ".jsonl" or p.resolve() == pathlib.Path(__file__).resolve():
            continue                  # this file necessarily contains the strings it searches for
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
            assert allowed.search(m.group(0)), f"{p.relative_to(ROOT)}: {m.group(0)}"
        assert "/Users/" not in text and "/home/" not in text, p.relative_to(ROOT)


def test_scrub_terms_are_absent_from_every_tracked_file():
    """Personal names cannot be written into the repo, even in this test, so the terms come from the
    environment:  SCRUB_TERMS="first last,handle" make scrub"""
    terms = [t.strip().lower() for t in os.environ.get("SCRUB_TERMS", "").split(",") if t.strip()]
    if not terms:
        pytest.skip("set SCRUB_TERMS to check for specific names")
    for p, text in text_files():
        low = text.lower()
        for t in terms:
            assert t not in low, f"{p.relative_to(ROOT)} contains a scrubbed term"


# ---- exposure -----------------------------------------------------------------------------------------------------------

def compose():
    return yaml.safe_load((ROOT / "infra" / "docker-compose.yml").read_text())


def test_gateway_port_defaults_to_loopback_and_never_to_all_interfaces():
    ports = compose()["services"]["gateway"]["ports"]
    assert len(ports) == 1
    assert ports[0].startswith("${GATEWAY_BIND:-127.0.0.1}:"), ports
    assert "0.0.0.0" not in (ROOT / "infra" / "docker-compose.yml").read_text().replace("Never 0.0.0.0", "")
    assert "GATEWAY_BIND=127.0.0.1" in (ROOT / "infra" / ".env.example").read_text()


def test_model_server_publishes_no_port_and_sits_on_the_internal_network_only():
    svc = compose()["services"]
    assert "ports" not in svc["ollama"] and svc["ollama"]["networks"] == ["internal"]
    assert compose()["networks"]["internal"]["internal"] is True
    assert set(svc["gateway"]["networks"]) == {"edge", "internal"}          # the only service with a way out


def test_gateway_container_is_locked_down():
    gw = compose()["services"]["gateway"]
    assert gw["read_only"] is True and gw["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in gw["security_opt"]
    assert "USER gw" in (ROOT / "gateway" / "Dockerfile").read_text()


def test_egress_script_allows_only_https_to_the_frontier_host():
    s = (ROOT / "infra" / "egress-allowlist.sh").read_text()
    assert "--dport 443" in s and "-j DROP" in s and "api.anthropic.com" in s


# ---- README contract ------------------------------------------------------------------------------------------------------

def test_readme_has_the_routing_table_verbatim_and_the_required_statements():
    text = (ROOT / "README.md").read_text()
    assert "## Routing behaviour" in text
    assert ROUTING_TABLE in text
    after = text.split(ROUTING_TABLE, 1)[1]
    assert "Taint always overrides consent and config" in after
    assert "can never reach the frontier lane" in after and "regardless of `auto_route_clean`, flags, or user action" in after
    assert "The default is `auto_route_clean: false`" in after
    assert "`/new` is the only way to clear taint" in after


def test_required_docs_and_make_targets_exist():
    for f in ("README.md", "docs/RUNBOOK.md", "docs/ONEPAGER.md", "infra/.env.example"):
        assert (ROOT / f).is_file(), f
    mk = (ROOT / "Makefile").read_text()
    for target in ("up:", "down:", "test:"):
        assert re.search(rf"^{target}", mk, re.M), target


def test_cpu_profile_model_server_is_as_locked_down_as_the_gpu_one():
    svc = compose()["services"]
    cpu = svc["ollama-cpu"]
    assert cpu["profiles"] == ["cpu"] and "ports" not in cpu
    assert list(cpu["networks"]) == ["internal"] and cpu["networks"]["internal"]["aliases"] == ["ollama"]
    assert "deploy" not in cpu                                  # no GPU reservation: it must start on any server
    assert cpu["volumes"] == svc["ollama"]["volumes"]           # same model volume
    assert "ollama" not in svc["gateway"].get("depends_on", {})  # nothing pins the gateway to one profile


def test_make_targets_cover_all_three_ways_to_start():
    mk = (ROOT / "Makefile").read_text()
    for target in ("up-cpu:", "models-cpu:", "up-mac:", "up:"):
        assert re.search(rf"^{target}", mk, re.M), target
    assert "PROFILES = --profile gpu --profile cpu" in mk and "$(COMPOSE) $(PROFILES) down" in mk


def test_model_downloader_is_a_one_shot_that_does_not_open_up_the_runtime_stack():
    """Regression: `make models` once failed because the model server has no route to the internet.
    Downloads now happen in a separate one-shot container. It must not weaken the running stack."""
    c = compose()
    svc, nets = c["services"], c["networks"]
    pull = svc["ollama-pull"]
    assert pull["profiles"] == ["pull"] and pull["restart"] == "no" and "ports" not in pull
    assert pull["networks"] == ["pull"] and not nets["pull"].get("internal")
    assert "ipam" not in nets["pull"]                      # not the fixed subnet the host firewall rule targets
    assert pull["volumes"] == svc["ollama"]["volumes"]     # writes into the same model volume
    for name in ("ollama", "ollama-cpu", "gateway"):        # nothing long-running is attached to it
        ns = svc[name]["networks"]
        assert "pull" not in (ns if isinstance(ns, list) else list(ns))
    mk = (ROOT / "Makefile").read_text()
    assert "--profile pull run --rm ollama-pull" in mk
    assert "exec ollama ollama pull" not in mk and "exec ollama-cpu ollama pull" not in mk
