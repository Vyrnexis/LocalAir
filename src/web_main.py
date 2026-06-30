"""Run the LocalAIR web server."""

from __future__ import annotations

import os

import uvicorn

from .config import DEFAULT_SERVER_PORT, load_config


def main() -> None:
    try:
        config = load_config()
        host = os.environ.get("LOCALAIR_HOST", config.server_host)
        port = int(os.environ.get("LOCALAIR_PORT", config.server_port))
    except Exception:
        host = os.environ.get("LOCALAIR_HOST", "0.0.0.0")
        port = int(os.environ.get("LOCALAIR_PORT", DEFAULT_SERVER_PORT))

    uvicorn.run("src.web:app", host=host, port=port)


if __name__ == "__main__":
    main()
