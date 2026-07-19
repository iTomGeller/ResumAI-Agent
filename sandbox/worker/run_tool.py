"""Sandbox worker entrypoint: reads one {"tool": ..., "args": {...}} JSON
object from stdin, executes exactly one allow-listed deterministic tool,
prints one JSON line.

The payload is parsed incrementally: as soon as the buffered bytes form a
complete JSON document the tool runs. This deliberately avoids depending on
stdin EOF, which the Docker daemon does not deliver to detached containers
when the attach stream closes (the cause of systematic tool timeouts).

Runs with network=none, read-only rootfs, non-root — no secrets, no docker
socket, no host paths. The tool implementations are copied verbatim from
workflow/app/runtime/sandbox_tools_local.py at image build time.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/sandbox")

from sandbox_tools import run_tool  # noqa: E402

MAX_PAYLOAD = 8 * 1024 * 1024


def read_payload() -> dict:
    buffer = b""
    while len(buffer) < MAX_PAYLOAD:
        chunk = os.read(0, 65536)
        if not chunk:  # EOF still honoured when it does arrive
            break
        buffer += chunk
        try:
            return json.loads(buffer.decode("utf-8"))
        except ValueError:
            continue
    return json.loads(buffer.decode("utf-8") or "{}")


def main() -> int:
    try:
        payload = read_payload()
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
