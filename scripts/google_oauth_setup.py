#!/usr/bin/env python3
"""One-time Google OAuth setup for Gmail/Calendar actions.

This needs a live browser login by the account owner - nothing else can obtain a refresh token.
Run it on a machine with a browser (or forward the printed URL and paste the result):

    python scripts/google_oauth_setup.py --client-id ... --client-secret ...

Prerequisite: create an OAuth 2.0 Client ID (type "Desktop app") in Google Cloud Console, with
the Gmail API and Calendar API enabled on the project, and add yourself as a test user if the
app is in "Testing" publish status.

Uses only httpx (already a project dependency) and the standard library - no google-* SDK.
"""
from __future__ import annotations

import argparse
import http.server
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send",
         "https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/calendar.events"]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.auth_code = qs.get("code", [None])[0]
        self.server.state_ok = qs.get("state", [None])[0] == self.server.expected_state
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"You can close this tab and return to the terminal.")

    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    redirect_uri = f"http://localhost:{a.port}/"
    state = secrets.token_urlsafe(16)
    params = {"client_id": a.client_id, "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state}
    url = AUTH_URL + "?" + urllib.parse.urlencode(params)

    with socketserver.TCPServer(("localhost", a.port), _Handler) as httpd:
        httpd.auth_code = None
        httpd.state_ok = False
        httpd.expected_state = state
        t = threading.Thread(target=httpd.handle_request, daemon=True)
        t.start()
        print(f"Opening your browser to sign in and grant access:\n  {url}\n")
        if not webbrowser.open(url):
            print("Could not open a browser automatically. Open the URL above yourself.")
        t.join(timeout=300)
        if not httpd.auth_code or not httpd.state_ok:
            print("No authorization code received (timed out or denied).", file=sys.stderr)
            return 1

    r = httpx.post(TOKEN_URL, data={"client_id": a.client_id, "client_secret": a.client_secret,
                                    "code": httpd.auth_code, "grant_type": "authorization_code",
                                    "redirect_uri": redirect_uri}, timeout=30)
    if r.status_code >= 400:
        print(f"Token exchange failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("No refresh_token in the response. Revoke prior access at "
             "https://myaccount.google.com/permissions and run this again "
             "(Google only issues a refresh token on first consent).", file=sys.stderr)
        return 1

    print("\nAdd these to infra/.env:\n")
    print(f"GOOGLE_CLIENT_ID={a.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={a.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
    print("\nThen add the tools you want under actions.enabled in infra/config.yaml and `make up`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
