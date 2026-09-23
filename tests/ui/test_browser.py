"""Drives the real UI in headless Chrome against the real gateway (stand-in models behind it).
Skipped automatically when Chrome or Node is not installed. Set CHROME_BIN to choose a browser."""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

HERE = pathlib.Path(__file__).resolve().parent
CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("chromium-browser") or "",
]


def chrome_path():
    return next((c for c in CHROME_CANDIDATES if c and os.path.exists(c)), None)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return True
        except OSError:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def tools():
    node, chrome = shutil.which("node"), chrome_path()
    if not node or not chrome:
        pytest.skip("needs node and a Chrome-family browser")
    return node, chrome


def run_scenario(tools, scenario, scope, tmp_path, server_file="stub_server.py"):
    node, chrome = tools
    app_port, dbg_port = free_port(), free_port()
    env = {**os.environ, "PORT": str(app_port), "SCOPE": scope}
    server = subprocess.Popen([sys.executable, str(HERE / server_file)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = tempfile.mkdtemp(prefix="chrome-profile-")
    browser = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                                "--no-default-browser-check", f"--user-data-dir={profile}",
                                f"--remote-debugging-port={dbg_port}", "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(f"http://127.0.0.1:{app_port}/health"), "gateway did not start"
        assert wait_for(f"http://127.0.0.1:{dbg_port}/json/version"), "browser did not start"
        out = tmp_path / "shots"
        out.mkdir()
        return subprocess.run([node, str(HERE / scenario), str(app_port), str(dbg_port), str(out)],
                              capture_output=True, text=True, timeout=180)
    finally:
        for p in (browser, server):
            p.terminate()
        for p in (browser, server):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(profile, ignore_errors=True)


def test_ui_session_scope_end_to_end_in_a_real_browser(tools, tmp_path):
    r = run_scenario(tools, "session_scope.mjs", "session", tmp_path)
    assert r.returncode == 0 and "CHECKS PASSED" in r.stdout, r.stdout[-3000:] + r.stderr[-1500:]


def test_ui_request_scope_end_to_end_in_a_real_browser(tools, tmp_path):
    r = run_scenario(tools, "request_scope.mjs", "request", tmp_path)
    assert r.returncode == 0 and "CHECKS PASSED" in r.stdout, r.stdout[-3000:] + r.stderr[-1500:]


def test_ui_natural_language_action_call_end_to_end_in_a_real_browser(tools, tmp_path):
    """Types a plain-English request into the real chat box and verifies the model's scripted
    reminder_create tool call round-trips through the confirmation dialog and executes - the
    capability this whole wiring pass was for, not just the request/session consent scenarios."""
    r = run_scenario(tools, "natural_language_tools.mjs", "session", tmp_path,
                     server_file="stub_server_actions.py")
    assert r.returncode == 0 and "CHECKS PASSED" in r.stdout, r.stdout[-3000:] + r.stderr[-1500:]
