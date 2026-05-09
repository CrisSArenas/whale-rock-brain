"""Entry point for the Whale Rock Brain dashboard.

Usage:
    python run_api.py
    # Then open in your browser:
    #   http://localhost:8000         -- the dashboard
    #   http://localhost:8000/docs    -- auto-generated Swagger docs
    #   http://localhost:8000/health  -- liveness probe
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import uvicorn  # noqa: E402

from whale_rock_brain.config import settings  # noqa: E402


HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    bar = "=" * 60
    print(f"\n{bar}")
    print("  Whale Rock Brain — alt-data research dashboard")
    print()
    print(f"    Dashboard:    http://localhost:{PORT}")
    print(f"    API docs:     http://localhost:{PORT}/docs")
    print(f"    Health check: http://localhost:{PORT}/health")
    print(f"{bar}\n")

    if not settings.anthropic_api_key.get_secret_value():
        print("WARNING: ANTHROPIC_API_KEY is not set. Refresh + chat will fail until you add it to .env.\n")

    uvicorn.run(
        "whale_rock_brain.api:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
