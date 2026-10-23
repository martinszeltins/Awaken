"""Find desktop autostart entries and enabled systemd services."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

GROUP_USER = "user"
GROUP_SYSTEM = "system"
GROUP_GNOME = "gnome"
GROUP_SYSTEMD_SYSTEM = "systemd-system"
GROUP_SYSTEMD_USER = "systemd-user"
GROUP_DOCKER = "docker"

GROUP_ORDER = (
    GROUP_USER,
    GROUP_SYSTEM,
    GROUP_GNOME,
    GROUP_SYSTEMD_SYSTEM,
    GROUP_SYSTEMD_USER,
    GROUP_DOCKER,
)

GROUP_TITLES = {
    GROUP_USER: "User autostart",
    GROUP_SYSTEM: "System autostart",
    GROUP_GNOME: "GNOME autostart",
    GROUP_SYSTEMD_SYSTEM: "System services",
    GROUP_SYSTEMD_USER: "User services",
    GROUP_DOCKER: "Docker",
}

SYSTEMD_DESCRIPTIONS = {
    GROUP_SYSTEMD_SYSTEM: "systemd system",
    GROUP_SYSTEMD_USER: "systemd user",
}

DOCKER_DESCRIPTION = "Containers that start with the Docker daemon"
DOCKER_CONTAINER_SOURCE = "Docker container"
_DOCKER_ACTIVE_STATUSES = {"running", "restarting", "paused"}


def docker_icon_path() -> str:
    """Return the shipped Docker icon, from the install prefix or the source tree."""
    module_dir = Path(__file__).resolve().parent
    candidates = (
        module_dir.parent / "docker-icon.png",
        module_dir.parents[1] / "docker-icon.png",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""

DESKTOP_GROUP = "Desktop Entry"
NO_EXECUTABLE = "No executable"
_REMOVED_FIELD_CODES = set("fFuUdDnNvm")
_ENV_OPTIONS_WITH_ARGUMENT = {"-u", "-C", "-S"}

SettingsLookup = Callable[[str, str], bool]
ExistsFn = Callable[[str], bool]
ProgramExistsFn = Callable[[str], bool]


@dataclass(frozen=True)
class StartupEntry:
    group: str
    name: str
    comment: str
    executable: str
    command_line: str
    source_path: str
    icon_name: str = ""
    icon_desktop_file: str = ""
    symlink_target: str = ""
    unit_id: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class EntryGroup:
    key: str
    title: str
    description: str
    entries: tuple[StartupEntry, ...]


@dataclass(frozen=True)
class ScanResult:
    entries: tuple[StartupEntry, ...]
    groups: tuple[EntryGroup, ...]
    systemd_error: str = ""
    docker_error: str = ""


@dataclass(frozen=True)
class DesktopRecord:
    filename: str
    group: str
    path: str
    entry_type: str
    hidden: bool
    gnome_enabled: bool
    try_exec: str
    only_show_in: str
    not_show_in: str
    autostart_condition: str
    name: str
    exec_line: str
    icon: str
    comment: str
    symlink_target: str
    dbus_activatable: bool


def in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID"))


def host_interface_settings() -> dict[str, str]:
    """Desktop settings from the host session.

    Inside Flatpak the sandbox has its own theme and accent color. The values
    here are what the native app follows.
    """
    if not in_flatpak():
        return {}
    try:
        completed = subprocess.run(
            host_argv(["gsettings", "list-recursively", "org.gnome.desktop.interface"]),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    values: dict[str, str] = {}
    prefix = "org.gnome.desktop.interface "
    for line in completed.stdout.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix) :]
        key, _, raw = rest.partition(" ")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == "'":
            raw = raw[1:-1]
        if key:
            values[key] = raw
    return values


def host_accent_color() -> str:
    """Accent color painted by the host GTK theme.

    Libadwaita inside Flatpak does not load the host theme, so the switch color
    is read from that theme and applied directly.
    """
    theme_name = host_interface_settings().get("gtk-theme", "")
    if not theme_name:
        return ""
    resource = f"/usr/share/themes/{theme_name}/gtk-4.0/gtk.gresource"
    try:
        listed = subprocess.run(
            host_argv(["gresource", "list", resource]),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    css_path = next((line for line in listed.stdout.splitlines() if line.endswith("/gtk.css")), "")
    if not css_path:
        return ""
    try:
        extracted = subprocess.run(
            host_argv(["gresource", "extract", resource, css_path]),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"@define-color\s+accent_color\s+(#[0-9A-Fa-f]{3,8})", extracted.stdout)
    return match.group(1) if match else ""


def apply_host_appearance() -> None:
    """Match the host accent color and color scheme before GTK starts."""
    settings = host_interface_settings()
    accent = settings.get("accent-color", "")
    if accent:
        os.environ["ADW_DEBUG_ACCENT_COLOR"] = accent
    scheme = settings.get("color-scheme", "")
    if scheme in {"prefer-dark", "prefer-light", "default"}:
        os.environ["ADW_DEBUG_COLOR_SCHEME"] = scheme


def host_argv(argv: Sequence[str]) -> list[str]:
    """Run a command on the host when this process is inside a Flatpak sandbox."""
    args = [str(part) for part in argv]
    if in_flatpak():
        return ["flatpak-spawn", "--host", *args]
    return args


def _flatpak_path_pairs() -> list[tuple[str, str]]:
    """Sandbox prefix, host prefix. Longer sandbox prefixes come first."""
    pairs = [
        ("/run/host/usr", "/usr"),
        ("/run/host/etc", "/etc"),
    ]
    app_id = os.environ.get("FLATPAK_ID", "")
    if app_id:
        sandbox_config = str(Path.home() / ".var" / "app" / app_id / "config")
        host_config = str(Path.home() / ".config")
        pairs.append((sandbox_config, host_config))
    return pairs


def to_host_path(path: str) -> str:
    """Show the host path for a file that was read through a Flatpak mount."""
    if not in_flatpak() or not path:
        return path
    for sandbox, host in _flatpak_path_pairs():
        if path == sandbox or path.startswith(sandbox + os.sep):
            return host + path[len(sandbox) :]
    return path


def to_sandbox_path(path: str) -> str:
    """Map a host path back to the Flatpak mount that this process can read."""
    if not in_flatpak() or not path:
        return path
    for sandbox, host in sorted(_flatpak_path_pairs(), key=lambda item: len(item[1]), reverse=True):
        if path == host or path.startswith(host + os.sep):
            return sandbox + path[len(host) :]
    return path


def host_file_visible(path: str) -> bool:
    """Whether a menu can offer this file.

    Flatpak can read host autostart and /usr through its mounts. Other host
    files, such as a systemd unit under the home directory, are still real:
    opening them goes through xdg-open on the host.
    """
    if not path:
        return False
    candidate = readable_path(path)
    if candidate != path or not in_flatpak():
        return os.path.isfile(candidate)
    if os.path.isfile(path):
        return True
    return os.path.isabs(path)


def readable_path(path: str) -> str:
    """Return a path this process can open.

    Inside Flatpak, the host path is what we show, and the sandbox mount is
    what we can actually read. Prefer the mount when both exist, because
    /etc and /usr inside the sandbox belong to the runtime.
    """
    if not path:
        return ""
    sandbox = to_sandbox_path(path)
    if sandbox != path and os.path.exists(sandbox):
        return sandbox
    return path


def display_path(path: str) -> str:
    """Replace the home directory prefix with ~ for display."""
    if not path:
        return ""
    home = str(Path.home())
    if path == home:
        return "~"
    prefix = home + os.sep
    if path.startswith(prefix):
        return "~" + path[len(home) :]
    return path


def split_desktop_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.split(";") if part]


def current_desktops() -> set[str]:
    raw = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return {part for part in raw.split(":") if part}


def desktop_visible(
    only_show_in: str | None,
    not_show_in: str | None,
    current: set[str],
) -> bool:
    """Return whether OnlyShowIn and NotShowIn allow this desktop."""
    only = split_desktop_list(only_show_in)
    hidden_on = split_desktop_list(not_show_in)
    if only and not current.intersection(only):
        return False
    if hidden_on and current.intersection(hidden_on):
        return False
    return True


def autostart_condition_allows(
    condition: str | None,
    *,
    settings_lookup: SettingsLookup | None = None,
    exists: ExistsFn = os.path.exists,
) -> bool:
    """Return False only when an AutostartCondition is known to be false.

    Unknown or unevaluable conditions keep the entry. GNOME will still decide
    at login, and hiding those entries would drop something that may start.
    """
    if condition is None:
        return True
    text = condition.strip()
    if not text:
        return True

    if text.startswith("GSettings "):
        rest = text[len("GSettings ") :].strip()
        schema, _, key = rest.partition(" ")
        key = key.split()[0] if key else ""
        if not schema or not key:
            return True
        lookup = settings_lookup or gio_settings_lookup
        try:
            return bool(lookup(schema, key))
        except (OSError, GLib.Error, LookupError, ValueError):
            return True

    if text.startswith("if-exists "):
        path = text[len("if-exists ") :].strip()
        if not path:
            return True
        return bool(exists(path))

    if text.startswith("unless-exists "):
        path = text[len("unless-exists ") :].strip()
        if not path:
            return True
        return not bool(exists(path))

    return True


def gio_settings_lookup(schema_id: str, key: str) -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None:
        raise LookupError("no GSettings schema source")
    schema = source.lookup(schema_id, True)
    if schema is None or not schema.has_key(key):
        raise LookupError(f"{schema_id} {key}")
    value = Gio.Settings.new(schema_id).get_value(key)
    if value.get_type_string() != "b":
        raise LookupError(key)
    return bool(value.get_boolean())


def expand_exec(
    exec_line: str,
    *,
    icon: str = "",
    name: str = "",
    desktop_path: str = "",
) -> str:
    """Replace desktop-entry field codes before shell parsing."""
    output: list[str] = []
    index = 0
    length = len(exec_line)
    while index < length:
        character = exec_line[index]
        if character != "%" or index + 1 >= length:
            output.append(character)
            index += 1
            continue
        code = exec_line[index + 1]
        index += 2
        if code == "%":
            output.append("%")
        elif code == "i" and icon:
            output.append(GLib.shell_quote("--icon"))
            output.append(" ")
            output.append(GLib.shell_quote(icon))
        elif code == "c" and name:
            output.append(GLib.shell_quote(name))
        elif code == "k" and desktop_path:
            output.append(GLib.shell_quote(desktop_path))
        elif code in _REMOVED_FIELD_CODES:
            continue
    return "".join(output)


def parse_exec(
    exec_line: str,
    *,
    icon: str = "",
    name: str = "",
    desktop_path: str = "",
) -> list[str]:
    if not exec_line or not exec_line.strip():
        return []
    expanded = expand_exec(
        exec_line,
        icon=icon,
        name=name,
        desktop_path=desktop_path,
    )
    if not expanded.strip():
        return []
    try:
        parsed = GLib.shell_parse_argv(expanded)
    except GLib.Error:
        return []
    argv = parsed[1] if isinstance(parsed, tuple) else parsed
    return list(argv)


def executable_from_argv(argv: Sequence[str]) -> str:
    """Return the program from an Exec argv, skipping a leading env wrapper."""
    args = list(argv)
    if not args:
        return ""
    if args[0] != "env":
        return args[0]

    args = args[1:]
    while args:
        token = args[0]
        if token == "--":
            args = args[1:]
            break
        if token.startswith("-"):
            if token in _ENV_OPTIONS_WITH_ARGUMENT and len(args) > 1:
                args = args[2:]
                continue
            args = args[1:]
            continue
        if "=" in token:
            args = args[1:]
            continue
        break
    return args[0] if args else ""


def resolve_program(program: str) -> str:
    if not program:
        return ""
    expanded = os.path.expanduser(program)
    if os.path.isabs(expanded) or os.sep in expanded:
        return os.path.normpath(expanded)
    if in_flatpak():
        host = _host_program(expanded)
        if host:
            return host
    found = GLib.find_program_in_path(expanded)
    return found or expanded


def program_exists(program: str) -> bool:
    if not program:
        return False
    expanded = os.path.expanduser(program)
    if os.path.isabs(expanded) or os.sep in expanded:
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return True
        sandbox = to_sandbox_path(expanded)
        return bool(
            sandbox != expanded
            and os.path.isfile(sandbox)
            and os.access(sandbox, os.X_OK)
        )
    if in_flatpak() and _host_program(expanded):
        return True
    return GLib.find_program_in_path(expanded) is not None


def _host_program(program: str) -> str:
    """Resolve a command name through the host /usr mounted inside Flatpak."""
    for directory in ("/run/host/usr/local/bin", "/run/host/usr/bin", "/run/host/bin"):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return to_host_path(candidate)
    return ""


def parse_execstart(text: str) -> list[tuple[str, str]]:
    """Return (path, argv) pairs from systemctl's ExecStart formatting."""
    commands: list[tuple[str, str]] = []
    if not text:
        return commands
    for block in _brace_blocks(text):
        fields: dict[str, str] = {}
        for part in block.split(" ; "):
            part = part.strip()
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key.strip()] = value.strip()
        path = fields.get("path", "")
        argv = fields.get("argv[]", "")
        if not path and argv:
            path = argv.split()[0]
        if path or argv:
            commands.append((path, argv))
    return commands


def is_template_unit(unit_name: str) -> bool:
    """True for uninstantiated templates such as getty@.service.

    systemctl show rejects those names. Instantiated units like
    getty@tty1.service still go through the normal property query.
    """
    service_name = unit_name[: -len(".service")] if unit_name.endswith(".service") else unit_name
    return service_name.endswith("@")


def strip_systemd_exec_prefix(command: str) -> str:
    """Drop systemd's leading ExecStart modifiers (-, @, +, !)."""
    text = command.strip()
    while text.startswith("!!") or (text and text[0] in "-@+!"):
        text = text[2:] if text.startswith("!!") else text[1:]
        text = text.lstrip()
    return text


def command_argv(command: str) -> list[str]:
    if not command or not command.strip():
        return []
    try:
        parsed = GLib.shell_parse_argv(command)
    except GLib.Error:
        return []
    argv = parsed[1] if isinstance(parsed, tuple) else parsed
    return list(argv)


def parse_systemctl_cat(text: str) -> tuple[str, str, list[str]]:
    """Return the unit path, description, and ExecStart lines from systemctl cat."""
    fragment = ""
    description = ""
    exec_lines: list[str] = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# /"):
            if not fragment:
                fragment = stripped[2:].strip()
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if not stripped or stripped.startswith(("#", ";")):
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if section == "Unit" and key == "Description" and value:
            description = value
        elif section == "Service" and key == "ExecStart":
            if value:
                exec_lines.append(strip_systemd_exec_prefix(value))
            else:
                exec_lines.clear()
    return fragment, description, exec_lines


def parse_unit_file_list(text: str) -> list[str]:
    units: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        name = stripped.split()[0]
        if name.endswith(".service"):
            units.append(name)
    return units


def parse_systemctl_show(text: str) -> list[dict[str, str]]:
    """Parse `systemctl show` output into one mapping per unit."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    exec_lines: list[str] = []

    def finish() -> None:
        nonlocal current, exec_lines
        if not current and not exec_lines:
            return
        if exec_lines:
            current["ExecStart"] = "\n".join(exec_lines)
        if current.get("Id"):
            records.append(current)
        current = {}
        exec_lines = []

    for line in text.splitlines():
        if not line.strip():
            finish()
            continue
        if line.startswith("ExecStart="):
            value = line[len("ExecStart=") :]
            if value:
                exec_lines.append(value)
            continue
        key, separator, value = line.partition("=")
        if separator and key:
            current[key] = value
    finish()
    return records


def desktop_autostart_enabled(record: DesktopRecord) -> bool:
    """True when GNOME will launch this desktop file."""
    return not record.hidden and record.gnome_enabled


def entry_is_listed(
    record: DesktopRecord,
    current: set[str],
    *,
    settings_lookup: SettingsLookup | None = None,
    exists: ExistsFn = os.path.exists,
    program_exists_fn: ProgramExistsFn = program_exists,
) -> bool:
    """True when the row belongs in the list, even if autostart is switched off."""
    if record.entry_type and record.entry_type != "Application":
        return False
    if record.try_exec and not program_exists_fn(record.try_exec):
        return False
    if not desktop_visible(record.only_show_in, record.not_show_in, current):
        return False
    return autostart_condition_allows(
        record.autostart_condition,
        settings_lookup=settings_lookup,
        exists=exists,
    )


def entry_is_active(
    record: DesktopRecord,
    current: set[str],
    *,
    settings_lookup: SettingsLookup | None = None,
    exists: ExistsFn = os.path.exists,
    program_exists_fn: ProgramExistsFn = program_exists,
) -> bool:
    if not desktop_autostart_enabled(record):
        return False
    return entry_is_listed(
        record,
        current,
        settings_lookup=settings_lookup,
        exists=exists,
        program_exists_fn=program_exists_fn,
    )


def select_desktop_records(
    records: Iterable[DesktopRecord],
    current: set[str],
    *,
    settings_lookup: SettingsLookup | None = None,
    exists: ExistsFn = os.path.exists,
    program_exists_fn: ProgramExistsFn = program_exists,
) -> list[DesktopRecord]:
    """Keep visible entries, letting an earlier directory claim a filename.

    A user file claims its name even when that file is disabled. The system
    copy is then suppressed, which is the XDG autostart rule. Disabled files
    stay in the list so they can be switched back on.
    """
    seen: set[str] = set()
    selected: list[DesktopRecord] = []
    for record in records:
        if record.filename in seen:
            continue
        seen.add(record.filename)
        if entry_is_listed(
            record,
            current,
            settings_lookup=settings_lookup,
            exists=exists,
            program_exists_fn=program_exists_fn,
        ):
            selected.append(record)
    return selected


def entry_matches(entry: StartupEntry, query: str) -> bool:
    text = query.strip().casefold()
    if not text:
        return True
    haystack = "\n".join(
        (
            entry.name,
            entry.executable,
            entry.source_path,
            display_path(entry.executable),
            display_path(entry.source_path),
        )
    ).casefold()
    return text in haystack


def autostart_directories() -> list[tuple[str, Path]]:
    if in_flatpak():
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return [
            (GROUP_USER, config_home / "autostart"),
            (GROUP_SYSTEM, Path("/run/host/etc/xdg/autostart")),
            (GROUP_GNOME, Path("/run/host/usr/share/gnome/autostart")),
        ]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [
        (GROUP_USER, config_home / "autostart"),
        (GROUP_SYSTEM, Path("/etc/xdg/autostart")),
        (GROUP_GNOME, Path("/usr/share/gnome/autostart")),
    ]


def scan_desktop(
    directories: Sequence[tuple[str, Path]] | None = None,
    *,
    current_desktops_set: set[str] | None = None,
    settings_lookup: SettingsLookup | None = None,
    exists: ExistsFn = os.path.exists,
    program_exists_fn: ProgramExistsFn = program_exists,
) -> list[StartupEntry]:
    directories = list(directories or autostart_directories())
    current = current_desktops() if current_desktops_set is None else current_desktops_set
    records: list[DesktopRecord] = []
    seen: set[str] = set()
    for group, directory in directories:
        for path in desktop_files(directory):
            filename = path.name
            if filename in seen:
                continue
            # The file claims this name even if it cannot be parsed or will not start.
            seen.add(filename)
            record = load_desktop_record(path, group)
            if record is None:
                continue
            records.append(record)
    selected = [
        record
        for record in records
        if entry_is_listed(
            record,
            current,
            settings_lookup=settings_lookup,
            exists=exists,
            program_exists_fn=program_exists_fn,
        )
    ]
    return [record_to_entry(record) for record in selected]


def container_autostarts(policy: str, status: str) -> bool:
    """True when Docker will start this container again after the daemon starts."""
    normalized = (policy or "no").lower()
    if normalized == "always":
        return True
    return normalized == "unless-stopped" and (status or "").lower() in _DOCKER_ACTIVE_STATUSES


def entries_from_docker_inspect(containers: list[dict]) -> list[StartupEntry]:
    """Group inspected containers into autostarting Compose projects and containers."""
    projects: dict[str, list[dict[str, str]]] = {}
    standalone: list[dict[str, str]] = []
    for container in containers:
        info = _docker_container_view(container)
        if info is None or not container_autostarts(info["policy"], info["status"]):
            continue
        if info["project"]:
            projects.setdefault(info["project"], []).append(info)
        else:
            standalone.append(info)

    entries: list[StartupEntry] = []
    for project, services in projects.items():
        services.sort(key=lambda item: (item["service"] or item["name"]).casefold())
        workdir = next((item["workdir"] for item in services if item["workdir"]), "")
        compose_file = next((item["config_file"] for item in services if item["config_file"]), "")
        comment = "\n".join(
            f"{item['service'] or item['name']}: {item['policy']}, {item['status']}"
            for item in services
        )
        entries.append(
            StartupEntry(
                group=GROUP_DOCKER,
                name=project,
                comment=comment,
                executable=workdir,
                command_line="",
                source_path=compose_file,
                icon_name=docker_icon_path(),
                enabled=True,
            )
        )
    for item in standalone:
        entries.append(
            StartupEntry(
                group=GROUP_DOCKER,
                name=item["name"],
                comment=f"{item['policy']}, {item['status']}",
                executable=item["image"],
                command_line="",
                source_path=DOCKER_CONTAINER_SOURCE,
                icon_name=docker_icon_path(),
                enabled=True,
            )
        )
    return entries


def scan_docker() -> tuple[list[StartupEntry], str]:
    try:
        listed = subprocess.run(
            host_argv(["docker", "ps", "-aq", "--no-trunc"]),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        if in_flatpak():
            return [], "Could not read Docker containers."
        return [], ""
    except subprocess.TimeoutExpired:
        return [], "Could not read Docker containers."
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "docker ps failed").strip()
        print(f"awaken: {detail}", flush=True)
        return [], "Could not read Docker containers."

    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if not container_ids:
        return [], ""

    inspected: list[dict] = []
    try:
        for start in range(0, len(container_ids), 50):
            chunk = container_ids[start : start + 50]
            result = subprocess.run(
                host_argv(["docker", "inspect", *chunk]),
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "docker inspect failed").strip()
                print(f"awaken: {detail}", flush=True)
                return [], "Could not read Docker containers."
            payload = json.loads(result.stdout or "[]")
            if isinstance(payload, list):
                inspected.extend(item for item in payload if isinstance(item, dict))
    except subprocess.TimeoutExpired:
        return [], "Could not read Docker containers."
    except (OSError, json.JSONDecodeError) as error:
        print(f"awaken: {error}", flush=True)
        return [], "Could not read Docker containers."
    return entries_from_docker_inspect(inspected), ""


def _docker_container_view(container: dict) -> dict[str, str] | None:
    config = container.get("Config")
    if not isinstance(config, dict):
        config = {}
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        labels = {}
    host = container.get("HostConfig")
    if not isinstance(host, dict):
        host = {}
    policy_info = host.get("RestartPolicy")
    if not isinstance(policy_info, dict):
        policy_info = {}
    state = container.get("State")
    if not isinstance(state, dict):
        state = {}
    name = str(container.get("Name") or "").lstrip("/")
    files = labels.get("com.docker.compose.project.config_files") or ""
    config_file = ""
    if isinstance(files, str):
        config_file = next((part.strip() for part in files.split(",") if part.strip()), "")
    return {
        "name": name,
        "image": str(config.get("Image") or ""),
        "policy": str(policy_info.get("Name") or "no"),
        "status": str(state.get("Status") or ""),
        "project": str(labels.get("com.docker.compose.project") or ""),
        "service": str(labels.get("com.docker.compose.service") or ""),
        "workdir": str(labels.get("com.docker.compose.project.working_dir") or ""),
        "config_file": config_file,
    }


def scan() -> ScanResult:
    directories = autostart_directories()
    entries = scan_desktop(directories)
    errors: list[str] = []
    for scope, group in (
        ("system", GROUP_SYSTEMD_SYSTEM),
        ("user", GROUP_SYSTEMD_USER),
    ):
        found, error = scan_systemd(scope, group)
        entries.extend(found)
        if error:
            print(f"awaken: {error}", flush=True)
            errors.append(_systemd_error_message(scope))
    docker_entries, docker_error = scan_docker()
    entries.extend(docker_entries)
    descriptions = {
        group: display_path(to_host_path(str(path))) for group, path in directories
    }
    descriptions.update(SYSTEMD_DESCRIPTIONS)
    descriptions[GROUP_DOCKER] = DOCKER_DESCRIPTION
    groups = build_groups(entries, descriptions)
    flat = tuple(entry for group in groups for entry in group.entries)
    return ScanResult(
        entries=flat,
        groups=tuple(groups),
        systemd_error=" ".join(errors),
        docker_error=docker_error,
    )


def build_groups(
    entries: Iterable[StartupEntry],
    descriptions: dict[str, str] | None = None,
) -> list[EntryGroup]:
    descriptions = descriptions or {}
    buckets: dict[str, list[StartupEntry]] = {key: [] for key in GROUP_ORDER}
    for entry in entries:
        if entry.group in buckets:
            buckets[entry.group].append(entry)
    groups: list[EntryGroup] = []
    for key in GROUP_ORDER:
        items = buckets[key]
        if not items:
            continue
        items.sort(key=lambda entry: entry.name.casefold())
        groups.append(
            EntryGroup(
                key=key,
                title=GROUP_TITLES[key],
                description=descriptions.get(key, ""),
                entries=tuple(items),
            )
        )
    return groups


def scan_systemd(scope: str, group: str) -> tuple[list[StartupEntry], str]:
    user = scope == "user"
    try:
        listed = _run_systemctl(
            user,
            [
                "list-unit-files",
                "--type=service",
                "--state=enabled,enabled-runtime",
                "--no-legend",
                "--plain",
            ],
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"systemd {scope}: {error}"
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "systemctl failed").strip()
        return [], f"systemd {scope}: {detail}"

    unit_names = parse_unit_file_list(listed.stdout)
    entries: list[StartupEntry] = []
    if unit_names:
        concrete = [name for name in unit_names if not is_template_unit(name)]
        templates = [name for name in unit_names if is_template_unit(name)]
        try:
            shown = _show_unit_properties(user, concrete)
        except (OSError, subprocess.TimeoutExpired) as error:
            return [], f"systemd {scope}: {error}"
        if shown is None:
            detail = "systemctl show failed"
            return [], f"systemd {scope}: {detail}"

        entries = entries_from_unit_records(shown, group)
        failures: list[str] = []
        for name in templates:
            try:
                catalog = _run_systemctl(user, ["cat", "--", name])
            except (OSError, subprocess.TimeoutExpired) as error:
                return [], f"systemd {scope}: {error}"
            entry = entry_from_unit_file(catalog.stdout, name, group) if catalog.returncode == 0 else None
            if entry is None:
                failures.append(name)
            else:
                entries.append(entry)
        if failures and not entries:
            return [], f"systemd {scope}: could not read {', '.join(failures)}"
        if failures:
            print(
                f"startup-applications: skipped {scope} units: {', '.join(failures)}",
                flush=True,
            )
    try:
        _merge_remembered_disabled_units(scope, user, group, entries)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"awaken: {error}", flush=True)
    return entries, ""


def entry_from_unit_file(
    text: str,
    unit_id: str,
    group: str,
    *,
    icon_for_unit: Callable[[str], tuple[str, str]] | None = None,
) -> StartupEntry | None:
    fragment, description, exec_lines = parse_systemctl_cat(text)
    if not fragment and not description and not exec_lines:
        return None
    commands: list[tuple[str, str]] = []
    for line in exec_lines:
        program = executable_from_argv(command_argv(line))
        if program:
            commands.append((resolve_program(program), line))
    if commands:
        executable = commands[0][0]
        command_line = "\n".join(line for _program, line in commands)
    else:
        executable = NO_EXECUTABLE
        command_line = ""
    icon_lookup = icon_for_unit or service_icon
    icon_name, icon_desktop = icon_lookup(unit_id)
    return StartupEntry(
        group=group,
        name=description or unit_id,
        comment="",
        executable=executable,
        command_line=command_line,
        source_path=to_host_path(fragment or unit_id),
        icon_name=icon_name,
        icon_desktop_file=icon_desktop,
        unit_id=unit_id,
        enabled=True,
    )


def entries_from_unit_records(
    text: str,
    group: str,
    *,
    icon_for_unit: Callable[[str], tuple[str, str]] | None = None,
    enabled: bool = True,
) -> list[StartupEntry]:
    icon_for_unit = icon_for_unit or service_icon
    entries: list[StartupEntry] = []
    seen_fragments: set[str] = set()
    seen_ids: set[str] = set()
    for record in parse_systemctl_show(text):
        unit_id = record.get("Id", "")
        fragment = record.get("FragmentPath", "")
        dedup_key = os.path.realpath(fragment) if fragment else ""
        if dedup_key:
            if dedup_key in seen_fragments:
                continue
            seen_fragments.add(dedup_key)
        elif unit_id:
            if unit_id in seen_ids:
                continue
            seen_ids.add(unit_id)
        else:
            continue

        commands = parse_execstart(record.get("ExecStart", ""))
        bus_name = record.get("BusName", "")
        if commands and commands[0][0]:
            executable = commands[0][0]
            if not os.path.isabs(executable):
                executable = resolve_program(executable)
            command_line = "\n".join(argv for _path, argv in commands if argv) or executable
        elif bus_name:
            executable = dbus_activated_label(bus_name)
            command_line = executable
        else:
            executable = NO_EXECUTABLE
            command_line = ""

        source = to_host_path(fragment or unit_id)
        symlink_target = ""
        link_path = readable_path(fragment) if fragment else ""
        if link_path and os.path.islink(link_path):
            symlink_target = to_host_path(os.path.realpath(link_path))
        icon_name, icon_desktop = icon_for_unit(unit_id)
        icon_desktop = to_host_path(icon_desktop)
        description = record.get("Description", "") or unit_id
        entries.append(
            StartupEntry(
                group=group,
                name=description,
                comment="",
                executable=executable,
                command_line=command_line,
                source_path=source,
                icon_name=icon_name,
                icon_desktop_file=icon_desktop,
                symlink_target=symlink_target,
                unit_id=unit_id,
                enabled=enabled,
            )
        )
    return entries


def service_icon(unit_id: str) -> tuple[str, str]:
    """Return a theme icon name and a desktop file that can provide it."""
    stem = unit_id[: -len(".service")] if unit_id.endswith(".service") else unit_id
    if "@" in stem:
        stem = stem.split("@", 1)[0]
    for directory in application_directories():
        candidate = directory / f"{stem}.desktop"
        if not candidate.is_file():
            continue
        icon_name = icon_name_from_desktop(candidate)
        return icon_name or stem, str(candidate)
    return stem, ""


def application_directories() -> list[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    directories = [data_home / "applications"]
    directories.extend(Path(data_dir) / "applications" for data_dir in data_dirs if data_dir)
    if in_flatpak():
        directories.extend(
            [
                Path("/run/host/usr/local/share/applications"),
                Path("/run/host/usr/share/applications"),
            ]
        )
    return directories


def docker_locations_visible(workdir: str, compose_file: str) -> tuple[bool, bool]:
    """Whether a Compose row can open its project directory and Compose file.

    Inside Flatpak those paths belong to the host. An absolute path is enough,
    because the sandbox cannot see the project even though xdg-open on the host can.
    """
    if in_flatpak():
        return os.path.isabs(workdir), os.path.isabs(compose_file)
    return os.path.isdir(workdir), os.path.isfile(compose_file)


def host_icon_directories() -> list[str]:
    if not in_flatpak():
        return []
    return [
        "/run/host/usr/local/share/icons",
        "/run/host/usr/share/icons",
        "/run/host/usr/share/pixmaps",
    ]


def icon_name_from_desktop(path: Path) -> str:
    keyfile = GLib.KeyFile()
    try:
        keyfile.load_from_file(str(path), GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return ""
    return _desktop_string(keyfile, "Icon")


def load_desktop_record(path: Path, group: str) -> DesktopRecord | None:
    keyfile = GLib.KeyFile()
    try:
        if not keyfile.load_from_file(str(path), GLib.KeyFileFlags.NONE):
            return None
    except GLib.Error:
        return None
    if not keyfile.has_group(DESKTOP_GROUP):
        return None

    name = _desktop_locale_string(keyfile, "Name") or path.stem
    symlink_target = os.path.realpath(path) if path.is_symlink() else ""
    return DesktopRecord(
        filename=path.name,
        group=group,
        path=str(path),
        entry_type=_desktop_string(keyfile, "Type"),
        hidden=_desktop_bool(keyfile, "Hidden", False),
        gnome_enabled=_desktop_bool(keyfile, "X-GNOME-Autostart-enabled", True),
        try_exec=_desktop_string(keyfile, "TryExec"),
        only_show_in=_desktop_string(keyfile, "OnlyShowIn"),
        not_show_in=_desktop_string(keyfile, "NotShowIn"),
        autostart_condition=_desktop_string(keyfile, "AutostartCondition"),
        name=name,
        exec_line=_desktop_string(keyfile, "Exec"),
        icon=_desktop_string(keyfile, "Icon"),
        comment=_desktop_locale_string(keyfile, "Comment"),
        symlink_target=symlink_target,
        dbus_activatable=_desktop_bool(keyfile, "DBusActivatable", False),
    )


def record_to_entry(record: DesktopRecord) -> StartupEntry:
    program = executable_from_argv(
        parse_exec(
            record.exec_line,
            icon=record.icon,
            name=record.name,
            desktop_path=record.path,
        )
    )
    if program:
        executable = resolve_program(program)
        command_line = record.exec_line.strip()
    elif record.dbus_activatable:
        executable = dbus_activated_label("")
        command_line = executable
    else:
        executable = NO_EXECUTABLE
        command_line = record.exec_line.strip()
    return StartupEntry(
        group=record.group,
        name=record.name,
        comment=record.comment,
        executable=executable,
        command_line=command_line,
        source_path=to_host_path(record.path),
        icon_name=record.icon,
        icon_desktop_file=to_host_path(record.path),
        symlink_target=to_host_path(record.symlink_target),
        enabled=desktop_autostart_enabled(record),
    )


def desktop_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [
        path
        for path in directory.iterdir()
        if path.name.lower().endswith(".desktop") and (path.is_file() or path.is_symlink())
    ]
    files.sort(key=lambda path: path.name.casefold())
    return files


def dbus_activated_label(bus_name: str) -> str:
    if bus_name:
        return f"D-Bus activated ({bus_name})"
    return "D-Bus activated"


def user_autostart_directory() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "autostart"


def set_desktop_autostart(
    source_path: str,
    enabled: bool,
    *,
    user_directory: Path | None = None,
) -> str:
    """Write the user autostart file and return its path.

    System files are copied into the user directory. A symlink there is replaced
    with a regular file so the link target is left unchanged.
    """
    source = Path(source_path)
    user_directory = user_directory or user_autostart_directory()
    user_directory.mkdir(parents=True, exist_ok=True)
    destination = user_directory / source.name
    keyfile = GLib.KeyFile()
    readable = Path(to_sandbox_path(str(source)))
    replacing_symlink = destination.is_symlink()
    if destination.is_symlink() or destination.is_file():
        load_from = destination
    elif readable.is_file():
        load_from = readable
    else:
        load_from = source
    try:
        if not keyfile.load_from_file(str(load_from), GLib.KeyFileFlags.KEEP_TRANSLATIONS):
            raise OSError(f"Could not read {load_from}")
    except GLib.Error as error:
        raise OSError(str(error)) from error
    if replacing_symlink:
        destination.unlink()
    if not keyfile.has_group(DESKTOP_GROUP):
        raise OSError(f"{load_from} has no desktop entry")
    keyfile.set_boolean(DESKTOP_GROUP, "Hidden", not enabled)
    keyfile.set_boolean(DESKTOP_GROUP, "X-GNOME-Autostart-enabled", enabled)
    try:
        if not keyfile.save_to_file(str(destination)):
            raise OSError(f"Could not write {destination}")
    except GLib.Error as error:
        raise OSError(str(error)) from error
    return to_host_path(str(destination))


def add_user_autostart(
    name: str,
    command: str,
    icon: str = "",
    *,
    user_directory: Path | None = None,
) -> str:
    """Create a new user autostart desktop file and return its path."""
    cleaned_name = name.strip()
    cleaned_command = command.strip()
    if not cleaned_name or not cleaned_command:
        raise OSError("Name and command are required")
    user_directory = user_directory or user_autostart_directory()
    user_directory.mkdir(parents=True, exist_ok=True)
    destination = user_directory / _autostart_filename(cleaned_name, user_directory)
    keyfile = GLib.KeyFile()
    keyfile.set_string(DESKTOP_GROUP, "Type", "Application")
    keyfile.set_string(DESKTOP_GROUP, "Name", cleaned_name)
    keyfile.set_string(DESKTOP_GROUP, "Exec", cleaned_command)
    cleaned_icon = icon.strip()
    if cleaned_icon:
        keyfile.set_string(DESKTOP_GROUP, "Icon", cleaned_icon)
    keyfile.set_boolean(DESKTOP_GROUP, "Hidden", False)
    keyfile.set_boolean(DESKTOP_GROUP, "X-GNOME-Autostart-enabled", True)
    try:
        if not keyfile.save_to_file(str(destination)):
            raise OSError(f"Could not write {destination}")
    except GLib.Error as error:
        raise OSError(str(error)) from error
    return to_host_path(str(destination))


def _autostart_filename(name: str, directory: Path) -> str:
    slug = []
    previous_dash = False
    for character in name.casefold():
        if character.isalnum():
            slug.append(character)
            previous_dash = False
        elif not previous_dash:
            slug.append("-")
            previous_dash = True
    stem = "".join(slug).strip("-") or "startup-application"
    candidate = f"{stem}.desktop"
    number = 2
    while (directory / candidate).exists() or (directory / candidate).is_symlink():
        candidate = f"{stem}-{number}.desktop"
        number += 1
    return candidate


def disabled_units_config_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "startup-applications" / "disabled-units.conf"


def read_disabled_units(path: Path | None = None) -> dict[str, list[str]]:
    path = path or disabled_units_config_path()
    result = {"system": [], "user": []}
    if not path.is_file():
        return result
    keyfile = GLib.KeyFile()
    try:
        keyfile.load_from_file(str(path), GLib.KeyFileFlags.NONE)
    except GLib.Error:
        return result
    for scope in ("system", "user"):
        try:
            values = keyfile.get_string_list(scope, "units")
        except GLib.Error:
            continue
        items = values[0] if isinstance(values, tuple) else values
        result[scope] = [item for item in items if item]
    return result


def write_disabled_units(scope: str, units: Sequence[str], path: Path | None = None) -> None:
    path = path or disabled_units_config_path()
    current = read_disabled_units(path)
    current[scope] = list(dict.fromkeys(units))
    if not any(current.values()):
        if path.exists():
            path.unlink()
        return
    keyfile = GLib.KeyFile()
    for name, values in current.items():
        if values:
            keyfile.set_string_list(name, "units", values)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not keyfile.save_to_file(str(path)):
            raise OSError(f"Could not write {path}")
    except GLib.Error as error:
        raise OSError(str(error)) from error


def remember_disabled_unit(scope: str, unit_id: str, path: Path | None = None) -> None:
    units = read_disabled_units(path)[scope]
    if unit_id not in units:
        units.append(unit_id)
    write_disabled_units(scope, units, path)


def forget_disabled_unit(scope: str, unit_id: str, path: Path | None = None) -> None:
    units = [name for name in read_disabled_units(path)[scope] if name != unit_id]
    write_disabled_units(scope, units, path)


def _merge_remembered_disabled_units(
    scope: str,
    user: bool,
    group: str,
    entries: list[StartupEntry],
) -> None:
    remembered = read_disabled_units().get(scope, [])
    if not remembered:
        return
    enabled_ids = {entry.unit_id for entry in entries if entry.unit_id}
    kept: list[str] = []
    for name in remembered:
        if name in enabled_ids:
            continue
        entry = _load_disabled_unit(user, name, group)
        if entry is None:
            continue
        entries.append(entry)
        kept.append(name)
    if kept != remembered:
        write_disabled_units(scope, kept)


def _load_disabled_unit(user: bool, unit_id: str, group: str) -> StartupEntry | None:
    if is_template_unit(unit_id):
        catalog = _run_systemctl(user, ["cat", "--", unit_id])
        if catalog.returncode != 0:
            return None
        entry = entry_from_unit_file(catalog.stdout, unit_id, group)
        return None if entry is None else replace(entry, enabled=False)

    shown = _show_unit_properties(user, [unit_id])
    if not shown:
        return None
    records = parse_systemctl_show(shown)
    if not records:
        return None
    if records[0].get("UnitFileState", "") in {"enabled", "enabled-runtime"}:
        return None
    loaded = entries_from_unit_records(shown, group, enabled=False)
    return loaded[0] if loaded else None


def _systemd_error_message(scope: str) -> str:
    if scope == "user":
        return "Could not read systemd user units."
    return "Could not read systemd system units."


def _show_unit_properties(user: bool, unit_names: Sequence[str]) -> str | None:
    """Return combined systemctl show output, or None when every query fails."""
    if not unit_names:
        return ""
    parts: list[str] = []
    any_success = False
    properties = [
        "show",
        "--no-pager",
        "-p",
        "Id",
        "-p",
        "Description",
        "-p",
        "FragmentPath",
        "-p",
        "ExecStart",
        "-p",
        "BusName",
        "-p",
        "UnitFileState",
    ]
    names = list(unit_names)
    for start in range(0, len(names), 80):
        chunk = names[start : start + 80]
        shown = _run_systemctl(user, [*properties, *chunk])
        if shown.returncode == 0:
            parts.append(shown.stdout)
            any_success = True
            continue
        if len(chunk) == 1:
            continue
        for name in chunk:
            one = _run_systemctl(user, [*properties, name])
            if one.returncode == 0:
                parts.append(one.stdout)
                any_success = True
    if not any_success:
        return None
    return "\n".join(parts)


def _run_systemctl(user: bool, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["systemctl"]
    if user:
        command.append("--user")
    command.extend(args)
    env = os.environ.copy()
    env["SYSTEMD_COLORS"] = "0"
    return subprocess.run(
        host_argv(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _brace_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    start: int | None = None
    depth = 0
    for index, character in enumerate(text):
        if character == "{":
            if depth == 0:
                start = index + 1
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start:index])
                start = None
    return blocks


def _desktop_string(keyfile: GLib.KeyFile, key: str) -> str:
    try:
        return keyfile.get_string(DESKTOP_GROUP, key) or ""
    except GLib.Error:
        return ""


def _desktop_locale_string(keyfile: GLib.KeyFile, key: str) -> str:
    try:
        return keyfile.get_locale_string(DESKTOP_GROUP, key, None) or ""
    except GLib.Error:
        return _desktop_string(keyfile, key)


def _desktop_bool(keyfile: GLib.KeyFile, key: str, default: bool) -> bool:
    try:
        return bool(keyfile.get_boolean(DESKTOP_GROUP, key))
    except GLib.Error:
        return default
