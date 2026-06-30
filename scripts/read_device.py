#!/usr/bin/env python3
"""Read LocalAIR's configured device and print one JSON result."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config
from src.device import TuyaDevice


def main() -> int:
    try:
        reading = TuyaDevice().get_status(load_config())
        result = {
            "temperature_c": reading.temperature_c,
            "humidity_percent": reading.humidity_percent,
            "ok": reading.ok,
        }
        if reading.error_message:
            result["error"] = reading.error_message
        print(json.dumps(result, separators=(",", ":")))
        return 0 if reading.ok else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "temperature_c": None,
                    "humidity_percent": None,
                    "ok": False,
                    "error": f"Configuration error ({type(exc).__name__}).",
                },
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
