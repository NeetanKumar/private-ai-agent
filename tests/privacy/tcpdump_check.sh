#!/usr/bin/env bash
# Live acceptance check for "audit log entry count == outbound connection count".
# Run as root ON THE GPU HOST while a workload of mixed queries is sent to the gateway.
#
#   sudo tests/privacy/tcpdump_check.sh <audit-log-path> <seconds> [bridge-interface]
#
# It counts new outbound TCP connections (SYN without ACK) leaving the gateway's edge subnet,
# and compares them with the number of audit records written during the same window.
# It also fails if ANY connection leaves for a destination other than the frontier host.
set -euo pipefail

AUDIT="${1:?audit log path}"; SECS="${2:?capture seconds}"; IFACE="${3:-any}"
SUBNET="${EDGE_SUBNET:-172.28.0.0/24}"; HOST="${FRONTIER_HOST:-api.anthropic.com}"
PCAP="$(mktemp)"; trap 'rm -f "$PCAP"' EXIT

before=$( [ -f "$AUDIT" ] && wc -l < "$AUDIT" || echo 0 )
timeout "$SECS" tcpdump -i "$IFACE" -nn -w "$PCAP" \
  "src net $SUBNET and tcp[tcpflags] & tcp-syn != 0 and tcp[tcpflags] & tcp-ack == 0" 2>/dev/null || true
after=$( [ -f "$AUDIT" ] && wc -l < "$AUDIT" || echo 0 )

allowed=$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u)
total=0; other=0
while read -r dst; do
  total=$((total+1))
  echo "$allowed" | grep -qx "$dst" || { other=$((other+1)); echo "UNEXPECTED destination: $dst"; }
done < <(tcpdump -nn -r "$PCAP" 2>/dev/null | sed -E 's/.* > ([0-9.]+)\.[0-9]+:.*/\1/')

records=$((after-before))
echo "outbound connections=$total audit records=$records unexpected=$other"
[ "$other" -eq 0 ] && [ "$total" -eq "$records" ] && echo PASS || { echo FAIL; exit 1; }
