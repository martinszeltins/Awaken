import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .const import APP_ID
from .scanner import host_accent_color, host_icon_directories, host_interface_settings
from .window import StartupWindow

_CSS = b"""
.startup-fallback {
  background-color: alpha(@window_fg_color, 0.08);
  border-radius: 8px;
  min-width: 32px;
  min-height: 32px;
}
"""


class Application(Adw.Application):
    def __init__(self, version: str) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.version = version
        self._add_action("quit", lambda *_args: self.quit(), ["<primary>q"])
        self._add_action("about", self._on_about)
        self.set_accels_for_action("win.refresh", ["F5"])
        self.set_accels_for_action("win.search", ["<primary>f"])
        self.set_accels_for_action("window.close", ["<primary>w"])

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name("Awaken")
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        display = Gdk.Display.get_default()
        if display is None:
            return
        Gtk.StyleContext.add_provider_for_display(
            display,
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        theme = Gtk.IconTheme.get_for_display(display)
        icon_dir = Path(__file__).resolve().parents[2] / "data" / "icons"
        if icon_dir.is_dir():
            theme.add_search_path(str(icon_dir))
        for path in host_icon_directories():
            if path.endswith("/pixmaps") or not os.path.isdir(path):
                continue
            theme.add_search_path(path)
        icon_theme_name = host_interface_settings().get("icon-theme", "")
        if icon_theme_name:
            Gtk.Settings.get_default().set_property("gtk-icon-theme-name", icon_theme_name)
        accent = host_accent_color()
        if accent:
            accent_provider = Gtk.CssProvider()
            accent_provider.load_from_data(
                f"switch:checked {{ background-color: {accent}; }}".encode()
            )
            Gtk.StyleContext.add_provider_for_display(
                display,
                accent_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER,
            )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = StartupWindow(application=self)
        window.present()

    def _on_about(self, *_args) -> None:
        dialog = Adw.AboutDialog(
            application_name="Awaken",
            application_icon=APP_ID,
            version=self.version,
            comments="See which applications and services start with the computer.",
        )
        dialog.present(self.props.active_window)

    def _add_action(self, name: str, callback, shortcuts: list[str] | None = None) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)
