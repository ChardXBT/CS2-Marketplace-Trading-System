#!/usr/bin/env bash
# Make git-crypt available before the repository is unlocked.
#
# Happy path: the vendored binary, checked against a pinned SHA-256, with no
# network access. It is /usr/bin/git-crypt from Ubuntu noble's
# git-crypt_0.7.0-0.1build3_amd64.deb (the package apt installs on
# ubuntu-24.04), whose hash chains to the signed noble InRelease. The upstream
# AGWA 0.7.0 Linux build links libcrypto.so.1.1, which ubuntu-24.04 lacks.
#
# Fallback: apt, hardened for the upstream DNS outage seen on hosted runners.
# This file must stay plaintext: it runs before git-crypt unlock.
set -uo pipefail

vendor_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../vendor" 2>/dev/null && pwd)"
vendor_binary="${vendor_dir:-/nonexistent}/git-crypt"
vendor_sha256="d0c01076d0bbfb4d17ab740aad29a7a661125e8551f2a5669a3dee6d394aa4bb"

if [[ -f "$vendor_binary" ]] \
  && printf '%s  %s\n' "$vendor_sha256" "$vendor_binary" | sha256sum --check --status \
  && chmod +x "$vendor_binary" \
  && vendor_version="$("$vendor_binary" --version 2>&1)"; then
  if [[ -n "${GITHUB_PATH:-}" ]]; then
    echo "$vendor_dir" >> "$GITHUB_PATH"
  fi
  export PATH="$vendor_dir:$PATH"
  echo "Using vendored ${vendor_version}."
  exit 0
fi
echo "::warning title=Vendored git-crypt unavailable::The vendored binary is missing, failed its SHA-256 check, or could not run; falling back to apt."

dns_failure_pattern="Temporary failure resolving|Could not resolve|Name or service not known"
apt_install() { timeout --kill-after=10s 45s sudo apt-get install -y git-crypt; }

doh_a_records() {
  # Every A record for a host, one per line. The resolvers are pinned by IP so
  # the workaround does not depend on the DNS service it works around.
  local host="$1" ips
  local parse='import json,sys; [print(a["data"]) for a in json.load(sys.stdin).get("Answer",[]) if a.get("type")==1]'
  ips=$(curl -4 -sf --max-time 10 --resolve dns.google:443:8.8.8.8 \
    "https://dns.google/resolve?name=$host&type=A" | python3 -c "$parse" 2>/dev/null)
  if [[ -z "$ips" ]]; then
    ips=$(curl -4 -sf --max-time 10 --resolve cloudflare-dns.com:443:1.1.1.1 \
      -H 'accept: application/dns-json' \
      "https://cloudflare-dns.com/dns-query?name=$host&type=A" | python3 -c "$parse" 2>/dev/null)
  fi
  printf '%s\n' "$ips"
}

pin_host() {
  local host="$1" ip ips
  getent hosts "$host" >/dev/null 2>&1 && return 0
  ips=$(doh_a_records "$host")
  for ip in $ips; do
    echo "$ip $host" | sudo tee -a /etc/hosts >/dev/null
  done
  if [[ -n "$ips" ]]; then
    echo "::notice::pinned $host -> $(echo $ips)"
  fi
}

out=$(apt_install 2>&1); rc=$?
if ((rc != 0)) && grep -qE "$dns_failure_pattern" <<< "$out"; then
  echo "::notice::apt DNS failure detected; resolving apt hosts via DoH and pinning /etc/hosts (upstream DNS outage workaround)"
  apt_hosts=$(grep -rhoE 'https?://[^/[:space:]"]+' /etc/apt/sources.list /etc/apt/sources.list.d/ /etc/apt/apt-mirrors.txt 2>/dev/null | sed -E 's|https?://||' | sort -u)
  for h in $apt_hosts azure.archive.ubuntu.com archive.ubuntu.com security.ubuntu.com; do
    pin_host "$h"
  done
  out=$(apt_install 2>&1); rc=$?
fi
if ((rc != 0)) && [[ -f /etc/apt/apt-mirrors.txt ]]; then
  echo "::notice::apt still failing; adding mirrors.edge.kernel.org as a last-resort mirror"
  pin_host mirrors.edge.kernel.org
  printf 'http://mirrors.edge.kernel.org/ubuntu/\tpriority:9\n' | sudo tee -a /etc/apt/apt-mirrors.txt >/dev/null
  timeout --kill-after=10s 90s sudo apt-get update >/dev/null 2>&1
  out=$(apt_install 2>&1); rc=$?
fi
printf '%s\n' "$out"
exit "$rc"
