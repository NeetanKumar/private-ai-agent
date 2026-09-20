#!/usr/bin/env bash
# Host-level egress allowlist for the GPU VM. Run as root; re-run every few minutes (cron or a
# systemd timer) because the frontier host's IPs can change.
#
# Effect: containers on the gateway's "edge" network (172.28.0.0/24) may open TCP 443 to the
# frontier API host and nothing else. Every other container sits on an `internal: true` network
# with no route out at all, so the gateway is the only component with outbound access.
set -euo pipefail

FRONTIER_HOST="${FRONTIER_HOST:-api.anthropic.com}"
EDGE_SUBNET="${EDGE_SUBNET:-172.28.0.0/24}"
CHAIN=PRIVATE_AI_EGRESS

ips=$(getent ahostsv4 "$FRONTIER_HOST" | awk '{print $1}' | sort -u)
[ -n "$ips" ] || { echo "could not resolve $FRONTIER_HOST; refusing to change rules" >&2; exit 1; }

iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for ip in $ips; do
  iptables -A "$CHAIN" -p tcp -d "$ip" --dport 443 -j ACCEPT
done
iptables -A "$CHAIN" -j DROP

# DOCKER-USER is evaluated for all container-forwarded traffic, before Docker's own rules.
iptables -C DOCKER-USER -s "$EDGE_SUBNET" -j "$CHAIN" 2>/dev/null || \
  iptables -I DOCKER-USER -s "$EDGE_SUBNET" -j "$CHAIN"
echo "egress from $EDGE_SUBNET limited to $FRONTIER_HOST: $(echo $ips | tr '\n' ' ')"
