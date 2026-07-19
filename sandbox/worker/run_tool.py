"""Sandbox worker entrypoint: reads {"tool": ..., "args": {...}} from stdin,
executes exactly one allow-listed deterministic tool, prints one JSON line.

Runs with network=none, read-only rootfs, non-root — no secrets, no docker
socket, no host paths. The tool implementations are copied verbatim from
workflow/app/runtime/sandbox_tools_local.py at image build time.
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, "/opt/sandbox")

from sandbox_tools import run_tool  # noqa: E402


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
        tool = str(payload.get("tool") or "")
        args = payload.get("args") or {}
        result = run_tool(tool, args)
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        return 0
    except Exception as exc:  # noqa: BLE001 - single JSON error line contract
        sys.stdout.write(json.dumps(
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
