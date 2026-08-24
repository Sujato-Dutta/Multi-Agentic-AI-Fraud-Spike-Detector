"""Start the API with an event loop psycopg can use.

Windows defaults to ``ProactorEventLoop``, which psycopg's async driver refuses. That makes
durable Postgres checkpointing impossible, and durable checkpoints are what allow analysts to
Approve or Modify a recommendation. The policy must be installed before the loop is created,
which is why this wrapper exists rather than a flag on ``uvicorn``.

Usage:
    python scripts/run_api.py                       # 127.0.0.1:8000
    python scripts/run_api.py --host 0.0.0.0 --port 9000 --reload
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def install_selector_event_loop() -> str:
    """Install a selector-based loop policy on Windows; return the policy in force."""

    if sys.platform != "win32":
        return type(asyncio.get_event_loop_policy()).__name__

    class SelectorPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self) -> asyncio.AbstractEventLoop:
            return asyncio.SelectorEventLoop(selectors.SelectSelector())

    asyncio.set_event_loop_policy(SelectorPolicy())
    return "SelectorEventLoop"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    loop_kind = install_selector_event_loop()
    print(f"event loop: {loop_kind} (psycopg-compatible: {loop_kind != 'ProactorEventLoop'})")

    import uvicorn

    uvicorn.run(
        "backend.app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        loop="asyncio",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
