#!/usr/bin/env python3
"""Compatibility entrypoint for the production runtime-diversity harness.

Prefer:
    python harness/verify_runtime_diversity.py --base http://<host>
"""

from harness.verify_runtime_diversity import main


if __name__ == "__main__":
    raise SystemExit(main())
