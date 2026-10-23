from __future__ import annotations

import os
import threading
from dataclasses import replace

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GioUnix", "2.0")

from gi.repository import Adw, Gdk, Gio, GioUnix, GLib, GObject, Gtk, Pango

from .scanner import (
    GROUP_DOCKER,
    GROUP_SYSTEMD_USER,
    NO_EXECUTABLE,
    StartupEntry,
    ScanResult,
    application_directories,
    display_path,
    docker_locations_visible,
    entry_matches,
    forget_disabled_unit,
    host_argv,
    in_flatpak,
    parse_exec,
    readable_path,
    host_file_visible,
    remember_disabled_unit,
    scan,
    set_desktop_autostart,
    add_user_autostart,
)

_FALLBACK_ICON = "application-x-executable-symbolic"
_GENERIC_PROGRAMS = {"sh", "bash", "dash", "env", "true", "false", "dbus-send"}


class StartupWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_title("Awaken")
        self.set_default_size(800, 680)
        self._result: ScanResult | None = None
        self._content: Adw.PreferencesPage | None = None
        self._scanning = False
        self._closed = False

        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        toolbar = Adw.ToolbarView()
        self._toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        self._search_button = Gtk.ToggleButton(icon_name="system-search-symbolic")
        self._search_button.set_tooltip_text("Search")
        self._spinner = Gtk.Spinner()
        self._spinner.set_visible(False)
        self._refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_button.set_tooltip_text("Refresh")
        self._refresh_button.connect("clicked", self._on_refresh)
        menu = Gio.Menu()
        menu.append("Keyboard Shortcuts", "win.shortcuts")
        menu.append("About Awaken", "app.about")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            primary=True,
            tooltip_text="Main menu",
        )
        header.pack_start(self._search_button)
        header.pack_end(menu_button)
        header.pack_end(self._refresh_button)
        header.pack_end(self._spinner)
        toolbar.add_top_bar(header)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search by name, executable, or path")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", lambda *_args: self._rebuild())
        search_bar = Gtk.SearchBar()
        search_bar.set_child(self._search_entry)
        search_bar.set_key_capture_widget(self)
        search_bar.connect_entry(self._search_entry)
        self._search_button.bind_property(
            "active",
            search_bar,
            "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
        )
        toolbar.add_top_bar(search_bar)

        self._stack = Gtk.Stack()
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)
        button_box = Gtk.Box()
        button_box.set_margin_top(18)
        button_box.set_margin_bottom(6)
        button_box.set_margin_start(12)
        button_box.set_margin_end(12)
        add_button = Gtk.Button(hexpand=True)
        add_button.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add"))
        add_button.set_tooltip_text("Add startup application")
        add_button.connect("clicked", self._on_add)
        button_box.append(add_button)
        clamp.set_child(button_box)
        content.append(clamp)
        content.append(self._stack)
        toolbar.set_content(content)

        loading = Adw.StatusPage(
            title="Looking for startup applications",
            description="Checking autostart directories and enabled services.",
        )
        loading_spinner = Adw.Spinner()
        loading_spinner.set_size_request(32, 32)
        loading.set_child(loading_spinner)
        self._stack.add_named(loading, "loading")

        self._empty = Adw.StatusPage(
            icon_name=_FALLBACK_ICON,
            title="No startup applications",
            description="Nothing in the autostart directories will start, and no enabled systemd services were found.",
        )
        self._stack.add_named(self._empty, "empty")
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="system-search-symbolic",
                title="No results",
                description="Try a different name, executable, or path.",
            ),
            "no-results",
        )

        self._add_window_action("refresh", self._on_refresh)
        self._add_window_action("search", self._on_search)
        self._add_window_action("shortcuts", self._on_shortcuts)
        self.connect("close-request", self._on_close)
        self._start_scan()

    def _add_window_action(self, name: str, callback) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def _on_close(self, *_args) -> bool:
        self._closed = True
        return False

    def _on_add(self, *_args) -> None:
        present_add_dialog(self)

    def _on_refresh(self, *_args) -> None:
        self._start_scan()

    def _on_search(self, *_args) -> None:
        self._search_button.set_active(True)
        self._search_entry.grab_focus()

    def _on_shortcuts(self, *_args) -> None:
        section = Adw.ShortcutsSection(title="General")
        section.add(Adw.ShortcutsItem(title="Search", accelerator="<Control>f"))
        section.add(Adw.ShortcutsItem(title="Refresh", accelerator="F5"))
        section.add(Adw.ShortcutsItem(title="Close window", accelerator="<Control>w"))
        section.add(Adw.ShortcutsItem(title="Quit", accelerator="<Control>q"))
        dialog = Adw.ShortcutsDialog()
        dialog.add(section)
        dialog.present(self)

    def _start_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._refresh_button.set_sensitive(False)
        self._spinner.set_visible(True)
        self._spinner.start()
        if self._result is None:
            self._stack.set_visible_child_name("loading")
        thread = threading.Thread(target=self._scan_thread, daemon=True)
        thread.start()

    def _scan_thread(self) -> None:
        try:
            result = scan()
        except Exception as error:  # noqa: BLE001 - show a failed scan instead of dying quietly
            result = ScanResult(entries=(), groups=(), systemd_error=str(error))
        GLib.idle_add(self._scan_finished, result)

    def _scan_finished(self, result: ScanResult) -> bool:
        if self._closed:
            return False
        try:
            self._scanning = False
            self._result = result
            self._refresh_button.set_sensitive(True)
            self._spinner.stop()
            self._spinner.set_visible(False)
            self._rebuild()
            self._show_scan_errors(result)
        except Exception as error:
            print(f"awaken: {error}", flush=True)
        return False

    def _rebuild(self) -> None:
        if self._result is None:
            self._stack.set_visible_child_name("loading")
            return

        query = self._search_entry.get_text()
        page = Adw.PreferencesPage()
        page.set_hexpand(True)
        page.set_vexpand(True)
        matched = 0
        for group in self._result.groups:
            rows = [entry for entry in group.entries if entry_matches(entry, query)]
            if not rows:
                continue
            matched += len(rows)
            preferences = Adw.PreferencesGroup(
                title=group.title,
                description=group.description,
            )
            for entry in rows:
                row = StartupRow(entry)
                row.connect("activated", self._on_row_activated)
                preferences.add(row)
            page.add(preferences)

        if self._content is not None:
            self._stack.remove(self._content)
        self._content = page
        self._stack.add_named(page, "content")
        if not self._result.entries:
            messages = [self._result.systemd_error, self._result.docker_error]
            message = " ".join(item for item in messages if item)
            if message:
                self._empty.set_description(message)
            else:
                self._empty.set_description(
                    "Nothing in the autostart directories will start, "
                    "and no enabled systemd services were found."
                )
            self._stack.set_visible_child_name("empty")
        elif matched == 0:
            self._stack.set_visible_child_name("no-results")
        else:
            self._stack.set_visible_child_name("content")

    def _on_row_activated(self, row: StartupRow) -> None:
        present_details(self, row.entry)

    def _show_scan_errors(self, result: ScanResult) -> None:
        for message in (result.systemd_error, result.docker_error):
            if message:
                self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=5))


class StartupRow(Adw.ActionRow):
    def __init__(self, entry: StartupEntry) -> None:
        super().__init__(
            title=entry.name,
            subtitle=_subtitle(entry),
            activatable=True,
        )
        self.entry = entry
        self.set_subtitle_lines(2)
        self.add_prefix(icon_widget(entry))
        options = self._options_button()
        if options is not None:
            self.add_suffix(options)
        if entry.group == GROUP_DOCKER:
            return
        self._switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=entry.enabled)
        self._switch.set_tooltip_text("Start automatically")
        self._switch.connect("state-set", self._on_state_set)
        self.add_suffix(self._switch)

    def _on_state_set(self, switch: Gtk.Switch, state: bool) -> bool:
        desired = bool(state)
        if self.entry.unit_id:
            switch.set_sensitive(False)
            self._set_systemd_enabled(desired)
            return True
        try:
            path = set_desktop_autostart(self.entry.source_path, desired)
        except OSError as error:
            self._toast(str(error))
            return True
        self.entry = replace(self.entry, enabled=desired, source_path=path)
        self.set_subtitle(_subtitle(self.entry))
        switch.set_state(desired)
        return True

    def _set_systemd_enabled(self, enabled: bool) -> None:
        scope = "user" if self.entry.group == GROUP_SYSTEMD_USER else "system"
        command = ["systemctl"]
        if scope == "user":
            command.append("--user")
        command.extend(["enable" if enabled else "disable", "--", self.entry.unit_id])
        try:
            process = Gio.Subprocess.new(
                command,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as error:
            self._finish_systemd_toggle(self.entry.enabled, str(error))
            return
        process.communicate_utf8_async(None, None, self._on_systemd_finished, (enabled, scope))

    def _on_systemd_finished(
        self,
        process: Gio.Subprocess,
        result: Gio.AsyncResult,
        user_data: tuple[bool, str],
    ) -> None:
        enabled, scope = user_data
        try:
            _ok, stdout, stderr = process.communicate_utf8_finish(result)
        except GLib.Error as error:
            self._finish_systemd_toggle(self.entry.enabled, str(error))
            return
        if not process.get_successful():
            detail = (stderr or stdout or "Could not change autostart").strip()
            message = detail.splitlines()[0] if detail else "Could not change autostart"
            self._finish_systemd_toggle(self.entry.enabled, message)
            return
        if enabled:
            forget_disabled_unit(scope, self.entry.unit_id)
        else:
            remember_disabled_unit(scope, self.entry.unit_id)
        self.entry = replace(self.entry, enabled=enabled)
        self._finish_systemd_toggle(enabled, "")

    def _finish_systemd_toggle(self, enabled: bool, error: str) -> None:
        self._switch.set_state(enabled)
        self._switch.set_sensitive(True)
        if error:
            self._toast(error)

    def _options_button(self) -> Gtk.MenuButton | None:
        if self.entry.group == GROUP_DOCKER:
            return self._docker_options_button()
        menu = Gio.Menu()
        menu.append("Open", "entry.open")
        if self._has_desktop_file():
            menu.append("View .desktop entry", "entry.view-desktop")
        if not self.entry.unit_id:
            menu.append("Open autostart directory", "entry.open-directory")
        if self.entry.unit_id and host_file_visible(self.entry.source_path):
            menu.append("Open unit file", "entry.view-unit")

        button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text="Options",
            valign=Gtk.Align.CENTER,
        )
        button.add_css_class("flat")
        actions = Gio.SimpleActionGroup()
        self._add_entry_action(actions, "open", self._open, can_launch(self.entry))
        if self._has_desktop_file():
            self._add_entry_action(actions, "view-desktop", self._view_desktop)
        if not self.entry.unit_id:
            self._add_entry_action(actions, "open-directory", self._open_directory)
        if self.entry.unit_id and host_file_visible(self.entry.source_path):
            self._add_entry_action(actions, "view-unit", self._view_unit)
        button.insert_action_group("entry", actions)
        button.set_menu_model(menu)
        return button

    def _docker_options_button(self) -> Gtk.MenuButton | None:
        workdir = self.entry.executable
        compose_file = self.entry.source_path
        # Compose paths come from the host Docker daemon. They are real on the
        # host even when this sandbox cannot see the project directory.
        has_directory, has_compose_file = docker_locations_visible(workdir, compose_file)
        if not has_directory and not has_compose_file:
            return None
        menu = Gio.Menu()
        if has_directory:
            menu.append("Open project directory", "entry.open-project")
        if has_compose_file:
            menu.append("View Compose file", "entry.view-compose")
        button = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            tooltip_text="Options",
            valign=Gtk.Align.CENTER,
        )
        button.add_css_class("flat")
        actions = Gio.SimpleActionGroup()
        if has_directory:
            self._add_entry_action(actions, "open-project", self._open_project_directory)
        if has_compose_file:
            self._add_entry_action(actions, "view-compose", self._view_compose)
        button.insert_action_group("entry", actions)
        button.set_menu_model(menu)
        return button

    def _view_compose(self, *_args) -> None:
        open_text_file(self._window(), self.entry.source_path)

    def _open_project_directory(self, *_args) -> None:
        open_directory(self._window(), self.entry.executable)

    def _add_entry_action(self, group: Gio.SimpleActionGroup, name: str, callback, enabled: bool = True) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.set_enabled(enabled)
        action.connect("activate", callback)
        group.add_action(action)

    def _has_desktop_file(self) -> bool:
        if self.entry.unit_id:
            return host_file_visible(self.entry.icon_desktop_file)
        return host_file_visible(self.entry.source_path)

    def _open(self, *_args) -> None:
        launch_entry(self._window(), self.entry)

    def _view_desktop(self, *_args) -> None:
        path = self.entry.icon_desktop_file if self.entry.unit_id else self.entry.source_path
        open_text_file(self._window(), path)

    def _open_directory(self, *_args) -> None:
        open_containing_folder(self._window(), self.entry.source_path)

    def _view_unit(self, *_args) -> None:
        open_text_file(self._window(), self.entry.source_path)

    def _window(self) -> Gtk.Widget | None:
        root = self.get_root()
        return root if isinstance(root, Gtk.Widget) else None

    def _toast(self, message: str) -> None:
        root = self.get_root()
        if not isinstance(root, StartupWindow):
            return
        text = " ".join(message.split())
        if len(text) > 160:
            text = text[:157] + "..."
        root._toast_overlay.add_toast(Adw.Toast(title=text or "Could not change autostart", timeout=5))


def _desktop_app_info(path: str):
    try:
        return GioUnix.DesktopAppInfo.new_from_filename(path)
    except (GLib.Error, TypeError):
        return None


def icon_widget(entry: StartupEntry) -> Gtk.Widget:
    image = _image_for_icon(icon_for_entry(entry))
    if image is not None:
        return image

    image = Gtk.Image()
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    image.set_from_icon_name(_FALLBACK_ICON)
    image.set_pixel_size(16)
    frame = Gtk.Box()
    frame.add_css_class("startup-fallback")
    frame.set_halign(Gtk.Align.CENTER)
    frame.set_valign(Gtk.Align.CENTER)
    frame.append(image)
    return frame


def _image_for_icon(gicon: Gio.Icon | None) -> Gtk.Image | None:
    if gicon is None:
        return None
    image = Gtk.Image()
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    image.set_pixel_size(32)
    path = _paintable_icon_path(gicon)
    if path:
        try:
            image.set_from_paintable(Gdk.Texture.new_from_filename(path))
            return image
        except GLib.Error:
            pass
    image.set_from_gicon(gicon)
    return image


def _paintable_icon_path(gicon: Gio.Icon) -> str:
    """Return a file Gtk can paint the same way in the native app and Flatpak."""
    if isinstance(gicon, Gio.FileIcon):
        file = gicon.get_file()
        path = file.get_path() if file is not None else ""
        return readable_path(path) if path else ""
    if not isinstance(gicon, Gio.ThemedIcon):
        return ""
    display = Gdk.Display.get_default()
    if display is None:
        return ""
    theme = Gtk.IconTheme.get_for_display(display)
    for name in gicon.get_names():
        if name.endswith("-symbolic") or not theme.has_icon(name):
            continue
        paintable = theme.lookup_icon(name, None, 32, 1, Gtk.TextDirection.NONE, 0)
        file = paintable.get_file() if paintable is not None else None
        path = file.get_path() if file is not None else ""
        if path:
            return path
    return ""


def icon_for_entry(entry: StartupEntry) -> Gio.Icon | None:
    fallback = None
    if (
        in_flatpak()
        and entry.icon_desktop_file
        and entry.icon_name
        and _theme_has_icon(entry.icon_name)
    ):
        return Gio.ThemedIcon.new(entry.icon_name)
    if entry.icon_desktop_file:
        info = _desktop_app_info(readable_path(entry.icon_desktop_file))
        if info is not None:
            fallback = info.get_icon()
            resolved = _file_icon(fallback)
            if resolved is not None:
                return resolved

    for name in _icon_names(entry):
        icon = _icon_named(name)
        if icon is not None:
            return icon
    return fallback


def _file_icon(icon: Gio.Icon | None) -> Gio.Icon | None:
    """Use a real file for icons the Flatpak theme cannot see."""
    if icon is None:
        return None
    if isinstance(icon, Gio.FileIcon):
        file = icon.get_file()
        path = file.get_path() if file is not None else ""
        if not path:
            return icon
        readable = readable_path(path)
        if os.path.exists(readable):
            return Gio.FileIcon.new(Gio.File.new_for_path(readable))
        return None if in_flatpak() else icon
    return icon


def _icon_names(entry: StartupEntry) -> list[str]:
    names: list[str] = []
    icon_name = entry.icon_name
    if icon_name:
        names.append(os.path.expanduser(icon_name))
    program = os.path.basename(entry.executable)
    if (
        program
        and program not in _GENERIC_PROGRAMS
        and entry.executable != NO_EXECUTABLE
        and not entry.executable.startswith("D-Bus")
    ):
        names.append(program)
        for suffix in ("-ui", "-daemon", "-bin", "-gtk", "-gtk3"):
            if program.endswith(suffix) and len(program) > len(suffix) + 1:
                names.append(program[: -len(suffix)])
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


def _icon_named(name: str) -> Gio.Icon | None:
    if os.path.isabs(name):
        candidate = readable_path(os.path.expanduser(name))
        if os.path.exists(candidate):
            return Gio.FileIcon.new(Gio.File.new_for_path(candidate))
        return None

    for directory in _application_dirs():
        candidate = os.path.join(directory, f"{name}.desktop")
        if not os.path.isfile(candidate):
            continue
        info = _desktop_app_info(candidate)
        icon = info.get_icon() if info is not None else None
        if icon is None:
            continue
        if isinstance(icon, Gio.FileIcon):
            return icon
        names = icon.get_names() if isinstance(icon, Gio.ThemedIcon) else [name]
        for themed_name in names:
            found = _pixmap_icon_file(themed_name) or _hicolor_icon_file(themed_name)
            if found:
                return Gio.FileIcon.new(Gio.File.new_for_path(found))
        return icon

    found = _pixmap_icon_file(name) or _hicolor_icon_file(name)
    if found:
        return Gio.FileIcon.new(Gio.File.new_for_path(found))

    if _theme_has_icon(name):
        return Gio.ThemedIcon.new(name)
    return None


def _theme_has_icon(name: str) -> bool:
    display = Gdk.Display.get_default()
    if display is None or not name or name.endswith("-symbolic"):
        return False
    return Gtk.IconTheme.get_for_display(display).has_icon(name)


def _pixmap_icon_file(name: str) -> str:
    for directory in _pixmap_dirs():
        for extension in (".png", ".svg", ".xpm"):
            candidate = os.path.join(directory, name + extension)
            if os.path.isfile(candidate):
                return candidate
    return ""


def _hicolor_icon_file(name: str) -> str:
    roots = ["/usr/share/icons/hicolor"]
    if in_flatpak():
        roots.insert(0, "/run/host/usr/share/icons/hicolor")
    sizes = (
        "scalable",
        "256x256",
        "128x128",
        "96x96",
        "64x64",
        "48x48",
        "32x32",
        "24x24",
        "22x22",
        "16x16",
    )
    for root in roots:
        for size in sizes:
            for extension in (".svg", ".png"):
                candidate = os.path.join(root, size, "apps", name + extension)
                if os.path.isfile(candidate):
                    return candidate
    return ""


def _application_dirs() -> list[str]:
    return [str(path) for path in application_directories()]


def _pixmap_dirs() -> list[str]:
    directories = ["/usr/share/pixmaps"]
    if in_flatpak():
        directories.insert(0, "/run/host/usr/share/pixmaps")
    return directories


def _subtitle(entry: StartupEntry) -> str:
    command = entry.executable
    if not entry.unit_id and entry.group != GROUP_DOCKER and entry.command_line:
        command = entry.command_line
    return f"{display_path(command)}\n{display_path(entry.source_path)}"


def can_launch(entry: StartupEntry) -> bool:
    program = entry.executable
    if entry.group == GROUP_DOCKER:
        return False
    if not program or program == NO_EXECUTABLE or program.startswith("D-Bus"):
        return False
    if "/libexec/" in program:
        return False
    if in_flatpak():
        return True
    if _launch_desktop_info(entry) is not None:
        return True
    return os.path.isfile(program) and os.access(program, os.X_OK)


def _host_launch_argv(entry: StartupEntry) -> list[str]:
    if entry.command_line and not entry.unit_id:
        argv = parse_exec(entry.command_line)
        if argv:
            return argv
    if entry.executable and os.path.isabs(entry.executable):
        return [entry.executable]
    return []


def launch_entry(parent: Gtk.Widget | None, entry: StartupEntry) -> None:
    if in_flatpak():
        argv = _host_launch_argv(entry)
        if not argv:
            _toast_on(parent, "Could not open this application")
            return
        try:
            Gio.Subprocess.new(host_argv(argv), Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            _toast_on(parent, str(error))
        return
    info = _launch_desktop_info(entry)
    if info is not None:
        try:
            if not info.launch([], None):
                _toast_on(parent, "Could not open this application")
        except GLib.Error as error:
            _toast_on(parent, str(error))
        return
    try:
        Gio.Subprocess.new([entry.executable], Gio.SubprocessFlags.NONE)
    except GLib.Error as error:
        _toast_on(parent, str(error))


def _open_on_host(parent: Gtk.Widget | None, path: str) -> bool:
    if not in_flatpak():
        return False
    try:
        Gio.Subprocess.new(host_argv(["xdg-open", path]), Gio.SubprocessFlags.NONE)
    except GLib.Error as error:
        _toast_on(parent, str(error))
    return True


def open_text_file(parent: Gtk.Widget | None, path: str) -> None:
    if _open_on_host(parent, path):
        return
    if not os.path.isfile(path):
        _toast_on(parent, "File not found")
        return
    editor = Gio.AppInfo.get_default_for_type("text/plain", False)
    if editor is None:
        _toast_on(parent, "No text editor is available")
        return
    try:
        if not editor.launch([Gio.File.new_for_path(path)], None):
            _toast_on(parent, "Could not open the file")
    except GLib.Error as error:
        _toast_on(parent, str(error))


def open_containing_folder(parent: Gtk.Widget | None, path: str) -> None:
    if in_flatpak():
        _open_on_host(parent, os.path.dirname(path) or path)
        return
    window = parent if isinstance(parent, Gtk.Window) else None
    launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))

    def finished(source: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
        try:
            source.open_containing_folder_finish(result)
        except GLib.Error as error:
            _toast_on(parent, str(error))

    launcher.open_containing_folder(window, None, finished)


def open_directory(parent: Gtk.Widget | None, path: str) -> None:
    if _open_on_host(parent, path):
        return
    if not os.path.isdir(path):
        _toast_on(parent, "Folder not found")
        return
    try:
        if not Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None):
            _toast_on(parent, "Could not open the folder")
    except GLib.Error as error:
        _toast_on(parent, str(error))


def _launch_desktop_info(entry: StartupEntry):
    if entry.unit_id:
        path = entry.icon_desktop_file
    else:
        path = entry.icon_desktop_file or entry.source_path
    if not path:
        return None
    path = readable_path(path)
    if not os.path.isfile(path):
        return None
    return _desktop_app_info(path)


def _toast_on(parent: Gtk.Widget | None, message: str) -> None:
    if not isinstance(parent, StartupWindow):
        return
    text = " ".join(message.split())
    if len(text) > 160:
        text = text[:157] + "..."
    parent._toast_overlay.add_toast(Adw.Toast(title=text or "Something went wrong", timeout=5))


def present_add_dialog(window: StartupWindow) -> None:
    dialog = Adw.Dialog(title="Add Startup Application", content_width=420)
    dialog.set_follows_content_size(True)
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    cancel = Gtk.Button(label="Cancel")
    cancel.connect("clicked", lambda *_args: dialog.close())
    add = Gtk.Button(label="Add")
    add.add_css_class("suggested-action")
    add.set_sensitive(False)
    header.pack_start(cancel)
    header.pack_end(add)
    toolbar.add_top_bar(header)

    form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    form.set_margin_top(18)
    form.set_margin_bottom(24)
    form.set_margin_start(18)
    form.set_margin_end(18)
    name_row = _hinted_entry("My App")
    command_row = _hinted_entry("/usr/bin/my-app")
    icon_row = _hinted_entry("~/Pictures/my-app.png")
    command_browse = _browse_button("Choose a command")
    icon_browse = _browse_button("Choose an icon")
    command_browse.connect("clicked", lambda *_args: _browse_command(window, command_row))
    icon_browse.connect("clicked", lambda *_args: _browse_icon(window, icon_row))
    form.append(_field("Name", name_row))
    form.append(_field("Command", command_row, command_browse))
    form.append(_field("Icon", icon_row, icon_browse))
    toolbar.set_content(form)

    def refresh_add(*_args) -> None:
        add.set_sensitive(bool(name_row.get_text().strip()) and bool(command_row.get_text().strip()))

    name_row.connect("changed", refresh_add)
    command_row.connect("changed", refresh_add)
    add.connect(
        "clicked",
        lambda *_args: _save_new_autostart(
            window,
            dialog,
            name_row.get_text(),
            command_row.get_text(),
            icon_row.get_text(),
        ),
    )
    dialog.set_child(toolbar)
    dialog.present(window)


def _hinted_entry(placeholder: str) -> Gtk.Entry:
    entry = Gtk.Entry(placeholder_text=placeholder, hexpand=True)
    return entry


def _field(title: str, entry: Gtk.Entry, browse: Gtk.Widget | None = None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("heading")
    box.append(label)
    if browse is None:
        box.append(entry)
        return box
    row = Gtk.Box(spacing=6)
    row.append(entry)
    row.append(browse)
    box.append(row)
    return box


def _browse_button(tooltip: str) -> Gtk.Button:
    button = Gtk.Button(icon_name="document-open-symbolic")
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text(tooltip)
    return button


def _browse_command(window: Gtk.Window, row: Gtk.Entry) -> None:
    dialog = Gtk.FileDialog(title="Select Command")
    dialog.open(window, None, _finish_file_browse, row)


def _browse_icon(window: Gtk.Window, row: Gtk.Entry) -> None:
    dialog = Gtk.FileDialog(title="Select Icon")
    image_filter = Gtk.FileFilter()
    image_filter.set_name("Images")
    for mime_type in ("image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif"):
        image_filter.add_mime_type(mime_type)
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(image_filter)
    dialog.set_filters(filters)
    dialog.set_default_filter(image_filter)
    dialog.open(window, None, _finish_file_browse, row)


def _finish_file_browse(
    dialog: Gtk.FileDialog,
    result: Gio.AsyncResult,
    row: Gtk.Entry,
) -> None:
    try:
        file = dialog.open_finish(result)
    except GLib.Error:
        return
    if file is not None and file.get_path():
        row.set_text(file.get_path())


def _save_new_autostart(
    window: StartupWindow,
    dialog: Adw.Dialog,
    name: str,
    command: str,
    icon: str,
) -> None:
    try:
        add_user_autostart(name, command, icon)
    except OSError as error:
        text = " ".join(str(error).split()) or "Could not add the startup application"
        window._toast_overlay.add_toast(Adw.Toast(title=text[:160], timeout=5))
        return
    dialog.close()
    window._toast_overlay.add_toast(Adw.Toast(title="Added startup application", timeout=3))
    window._start_scan()


def present_details(parent: Gtk.Widget, entry: StartupEntry) -> None:
    dialog = Adw.Dialog(title=entry.name, content_width=520, content_height=640)
    dialog.set_follows_content_size(True)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    title_box = Gtk.Box(spacing=12)
    title = Gtk.Label(label=entry.name)
    title.set_ellipsize(Pango.EllipsizeMode.END)
    title.set_max_width_chars(28)
    title.add_css_class("title")
    title_box.append(icon_widget(entry))
    title_box.append(title)
    header.set_title_widget(title_box)
    toolbar.add_top_bar(header)

    page = Adw.PreferencesPage()
    page.set_size_request(480, -1)
    group = Adw.PreferencesGroup()
    if entry.comment:
        comment_title = "Comment"
        if entry.group == GROUP_DOCKER:
            comment_title = (
                "Services"
                if docker_locations_visible(entry.executable, entry.source_path)[1]
                else "Restart policy"
            )
        comment = Adw.ActionRow(title=comment_title, subtitle=entry.comment)
        comment.set_subtitle_lines(8)
        group.add(comment)
    if entry.unit_id and entry.unit_id != entry.name:
        group.add(Adw.ActionRow(title="Unit", subtitle=entry.unit_id))
    overlay = Adw.ToastOverlay()
    if entry.group == GROUP_DOCKER and docker_locations_visible(entry.executable, entry.source_path)[1]:
        _add_copy_row(group, overlay, parent, "Project directory", entry.executable)
        _add_copy_row(group, overlay, parent, "Compose file", entry.source_path)
    elif entry.group == GROUP_DOCKER:
        _add_copy_row(group, overlay, parent, "Image", entry.executable)
        if entry.source_path:
            group.add(Adw.ActionRow(title="Source", subtitle=entry.source_path))
    else:
        _add_copy_row(group, overlay, parent, "Executable", entry.executable)
        if entry.command_line and entry.command_line != entry.executable:
            _add_copy_row(group, overlay, parent, "Command", entry.command_line)
        _add_copy_row(group, overlay, parent, "Autostart location", entry.source_path)
        if entry.symlink_target and entry.symlink_target != entry.source_path:
            _add_copy_row(group, overlay, parent, "Links to", entry.symlink_target)
    page.add(group)
    overlay.set_child(page)
    toolbar.set_content(overlay)
    dialog.set_child(toolbar)
    dialog.present(parent)


def _add_copy_row(
    group: Adw.PreferencesGroup,
    overlay: Adw.ToastOverlay,
    parent: Gtk.Widget,
    title: str,
    value: str,
) -> None:
    if not value or value == NO_EXECUTABLE:
        if value == NO_EXECUTABLE:
            group.add(Adw.ActionRow(title=title, subtitle=value))
        return
    row = Adw.ActionRow(title=title, subtitle=value)
    row.set_subtitle_selectable(True)
    row.set_subtitle_lines(6)
    button = Gtk.Button(icon_name="edit-copy-symbolic")
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text(f"Copy {title.lower()}")
    button.connect("clicked", lambda *_args: _copy(parent, overlay, value))
    row.add_suffix(button)
    group.add(row)


def _copy(parent: Gtk.Widget, overlay: Adw.ToastOverlay, text: str) -> None:
    parent.get_clipboard().set(text)
    overlay.add_toast(Adw.Toast(title="Copied", timeout=2))
