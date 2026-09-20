#!/usr/bin/env bash
# Usage: tests/test_portscan.sh <public-ip-or-tailnet-name>
# Run FROM A DIFFERENT MACHINE than the GPU host. Expect only SSH (22) open, and nothing for 8080/11434.
set -euo pipefail
host="${1:?usage: $0 <host>}"
nmap -Pn -p 1-65535 --open "$host"
for p in 8080 11434; do
  if nc -z -w 3 "$host" "$p" 2>/dev/null; then echo "FAIL: port $p reachable"; exit 1; fi
done
echo "PASS: gateway and model ports not reachable from outside"
