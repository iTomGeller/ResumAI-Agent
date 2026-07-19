"""Compatibility entrypoint for the canonical fresh-volume ECS deployment.

Historically this file implemented a second, divergent deployment path with
embedded default passwords and incomplete service configuration.  Keeping two
implementations made it too easy to reintroduce SQL/volume migration.  The
canonical implementation is now ``deploy_aliyun.py``; this wrapper preserves
the old command name without duplicating deployment logic.
"""
from __future__ import annotations

from deploy_aliyun import main


if __name__ == "__main__":
    main()
