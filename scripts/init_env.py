"""Create infra/.env from the example if missing and fill in a random gateway token.

Never overwrites a value that is already set. The file is created with mode 600 and is git-ignored.
"""
import pathlib
import secrets
import shutil
import sys

root = pathlib.Path(__file__).resolve().parents[1]
env, example = root / "infra" / ".env", root / "infra" / ".env.example"

if not env.exists():
    shutil.copy(example, env)
    print(f"created {env.relative_to(root)}")
lines = env.read_text().splitlines()
changed = False
for i, line in enumerate(lines):
    if line.startswith("GATEWAY_TOKEN_OWNER=") and not line.split("=", 1)[1].strip():
        lines[i] = "GATEWAY_TOKEN_OWNER=" + secrets.token_hex(32)
        changed = True
        print("generated a token for user 'owner' (see infra/.env)")
if changed:
    env.write_text("\n".join(lines) + "\n")
env.chmod(0o600)
if not any(l.startswith("GATEWAY_TOKEN_OWNER=") and l.split("=", 1)[1].strip() for l in lines):
    sys.exit("GATEWAY_TOKEN_OWNER is missing from infra/.env")
