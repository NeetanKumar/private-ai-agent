"""Same guardrail as gateway/tools.py's read-only allowlist, applied to the action-tool
allowlist: it lives in code, config can only narrow it, and the shipped default is empty."""
import pytest
import yaml

from conftest import ROOT
from gateway.config import Settings


def test_config_cannot_enable_an_unknown_action():
    raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
    for bad in ("send_wire_transfer", "delete_account", "shutdown_server"):
        raw["actions"]["enabled"] = ["reminder_list", bad]
        with pytest.raises(ValueError):
            Settings(**raw)


def test_shipped_default_has_no_actions_enabled():
    raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
    assert raw["actions"]["enabled"] == []
    assert Settings(**raw).actions.enabled == []


def test_shipped_default_has_no_source_registered_clean_for_google_content():
    """The operator may choose to register tool:gmail_read etc. as CLEAN themselves (a
    materially riskier choice than this project's PRIVATE-by-default rule), but the shipped
    config must never make that choice on their behalf."""
    raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
    google_sources = [k for k in (raw.get("sources") or {}) if "gmail" in k or "calendar" in k]
    assert google_sources == []


def test_oauth_google_config_holds_env_var_names_only_never_secrets():
    raw = yaml.safe_load((ROOT / "infra" / "config.yaml").read_text())
    og = raw["actions"]["oauth_google"]
    for key in ("client_id_env", "client_secret_env", "refresh_token_env"):
        # a real secret would not look like a plain UPPER_SNAKE env var name
        assert og[key].isupper() and "_" in og[key] and len(og[key]) < 40
