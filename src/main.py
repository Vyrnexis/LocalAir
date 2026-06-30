"""LocalAir application entry point."""

from __future__ import annotations

import os
import sys

# Some Linux graphics drivers intermittently fail to repaint GTK text after
# asynchronous widget updates. Cairo is slower than the accelerated renderer,
# but this small dashboard benefits more from deterministic rendering.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk

from .config import AppConfig, load_config
from .device import TuyaDevice
from .ui import LocalAirWindow, install_css


class LocalAirApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id="io.github.localair.LocalAir",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.window: LocalAirWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            install_css()
            try:
                config = load_config()
                config_error = None
            except ValueError as exc:
                config = AppConfig()
                config_error = str(exc)

            self.window = LocalAirWindow(
                application=self,
                config=config,
                device=TuyaDevice(),
                startup_error=config_error,
            )
        self.window.present()


def main() -> int:
    return LocalAirApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
