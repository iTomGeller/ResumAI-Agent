#!/bin/sh
set -eu

# Mainland ECS can reach exa.ai while Cloudflare's DNS answers for
# mcp.exa.ai/api.exa.ai time out. Resolve Exa's reachable edge dynamically so
# TLS still uses the official Exa hostnames and certificates. An explicit
# CONNECT proxy always takes precedence over this route workaround.
if [ -z "${EXA_HTTPS_PROXY:-}" ]; then
  exa_edge_ip="$(getent ahostsv4 exa.ai 2>/dev/null | head -n 1 | cut -d ' ' -f 1 || true)"
  if [ -n "$exa_edge_ip" ]; then
    printf '%s mcp.exa.ai api.exa.ai\n' "$exa_edge_ip" >> /etc/hosts
  fi
fi

exec "$@"
