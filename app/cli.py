"""Console entry point: run the SIGNAL-X dashboard.

Installed as the ``signal-x`` command via pyproject [project.scripts].
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="signal-x",
        description="SIGNAL-X observation dashboard (FastAPI + live SSE)",
    )
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = ap.parse_args(argv)

    import uvicorn

    uvicorn.run("app.server:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
