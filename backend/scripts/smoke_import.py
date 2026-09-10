#!/usr/bin/env python
"""Import the app and the worker, and print the route table.

Exists because a route can be syntactically perfect and still blow up at import
time - FastAPI validates decorator arguments when the module loads, not when a
request arrives. `python -m compileall` does not catch that; only an actual
import does.

    python scripts/smoke_import.py

Needs no database, Redis or API key: create_engine is lazy and nothing connects
at import. Run it before every deploy.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", "smoke-test")


def main() -> int:
    try:
        from app.main import app
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  app.main did not import: {type(exc).__name__}: {exc}")
        raise

    routes = [
        (r.path, sorted(m for m in r.methods if m != "HEAD"))
        for r in app.routes
        if hasattr(r, "methods")
    ]
    for path, methods in sorted(routes):
        print(f"  {','.join(methods):<10} {path}")
    print(f"\nOK  app.main imported, {len(routes)} routes")

    try:
        from app.worker.tasks import celery
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  worker did not import: {type(exc).__name__}: {exc}")
        raise

    tasks = sorted(n for n in celery.tasks if not n.startswith("celery."))
    print(f"OK  worker imported, tasks: {', '.join(tasks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
