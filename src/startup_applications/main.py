import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from .application import Application
from .const import VERSION
from .scanner import apply_host_appearance


def main(version: str | None = None) -> int:
    apply_host_appearance()
    app = Application(version or VERSION)
    return app.run(sys.argv)
