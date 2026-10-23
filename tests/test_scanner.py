import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from startup_applications.scanner import (
    GROUP_DOCKER,
    GROUP_GNOME,
    GROUP_SYSTEM,
    GROUP_SYSTEMD_SYSTEM,
    GROUP_USER,
    DesktopRecord,
    NO_EXECUTABLE,
    autostart_condition_allows,
    build_groups,
    dbus_activated_label,
    desktop_visible,
    display_path,
    entries_from_unit_records,
    entry_from_unit_file,
    entry_is_active,
    entry_matches,
    executable_from_argv,
    expand_exec,
    forget_disabled_unit,
    is_template_unit,
    parse_exec,
    parse_execstart,
    parse_systemctl_cat,
    parse_systemctl_show,
    parse_unit_file_list,
    read_disabled_units,
    record_to_entry,
    remember_disabled_unit,
    resolve_program,
    scan_desktop,
    select_desktop_records,
    set_desktop_autostart,
    add_user_autostart,
    entries_from_docker_inspect,
    container_autostarts,
    autostart_directories,
    docker_locations_visible,
    host_file_visible,
    readable_path,
    to_host_path,
    to_host_path,
    to_sandbox_path,
)


def record(**overrides) -> DesktopRecord:
    values = {
        "filename": "app.desktop",
        "group": GROUP_USER,
        "path": "/tmp/app.desktop",
        "entry_type": "Application",
        "hidden": False,
        "gnome_enabled": True,
        "try_exec": "",
        "only_show_in": "",
        "not_show_in": "",
        "autostart_condition": "",
        "name": "App",
        "exec_line": "/usr/bin/app",
        "icon": "",
        "comment": "",
        "symlink_target": "",
        "dbus_activatable": False,
    }
    values.update(overrides)
    return DesktopRecord(**values)


class ExecTests(unittest.TestCase):
    def test_strips_field_codes_and_keeps_program(self):
        argv = parse_exec("/usr/bin/app %u %F")
        self.assertEqual(argv, ["/usr/bin/app"])
        self.assertEqual(executable_from_argv(argv), "/usr/bin/app")

    def test_parses_quoted_program_with_spaces(self):
        argv = parse_exec('"/usr/bin/my app" --flag')
        self.assertEqual(argv, ["/usr/bin/my app", "--flag"])

    def test_expands_icon_name_and_percent(self):
        expanded = expand_exec("foo %i %%", icon="my icon")
        argv = parse_exec("foo %i %%", icon="my icon")
        self.assertIn("--icon", argv)
        self.assertIn("my icon", argv)
        self.assertEqual(argv[0], "foo")
        self.assertTrue(expanded.endswith("%") or "% " in expanded or expanded.endswith("'%'"))
        self.assertEqual(argv[-1], "%")

    def test_expands_translated_name_and_desktop_path(self):
        argv = parse_exec(
            "app %c %k",
            name="My App",
            desktop_path="/tmp/my app.desktop",
        )
        self.assertEqual(argv, ["app", "My App", "/tmp/my app.desktop"])

    def test_empty_and_unbalanced_quotes(self):
        self.assertEqual(parse_exec("   "), [])
        self.assertEqual(parse_exec("%f %U"), [])
        self.assertEqual(parse_exec('"'), [])

    def test_env_wrapper_is_skipped(self):
        argv = parse_exec("env -u FOO BAR=1 /usr/bin/app --flag")
        self.assertEqual(executable_from_argv(argv), "/usr/bin/app")
        argv = parse_exec("env EXAMPLE=1 /usr/bin/from-env")
        self.assertEqual(executable_from_argv(argv), "/usr/bin/from-env")

    def test_resolve_program(self):
        self.assertEqual(resolve_program(""), "")
        self.assertEqual(resolve_program("/usr/bin/ls"), "/usr/bin/ls")
        self.assertEqual(
            resolve_program("~/startup-applications-test-bin"),
            os.path.normpath(os.path.expanduser("~/startup-applications-test-bin")),
        )
        self.assertEqual(
            resolve_program("definitely-not-a-program-xyz"),
            "definitely-not-a-program-xyz",
        )
        resolved = resolve_program("ls")
        self.assertTrue(os.path.isabs(resolved))
        self.assertTrue(resolved.endswith("ls"))


class ExecStartTests(unittest.TestCase):
    def test_parses_path_and_argv(self):
        text = (
            "{ path=/usr/bin/anydesk ; argv[]=/usr/bin/anydesk --service ; "
            "ignore_errors=no ; start_time=[Thu 2026-10-22 19:48:26 EEST] ; "
            "stop_time=[n/a] ; pid=2633 ; code=(null) ; status=0/0 }"
        )
        self.assertEqual(
            parse_execstart(text),
            [("/usr/bin/anydesk", "/usr/bin/anydesk --service")],
        )

    def test_multiple_blocks_and_empty_text(self):
        text = (
            "{ path=/bin/b ; argv[]=/bin/b ; ignore_errors=no }\n"
            "{ path=/bin/c ; argv[]=/bin/c extra ; ignore_errors=no }"
        )
        self.assertEqual(
            parse_execstart(text),
            [("/bin/b", "/bin/b"), ("/bin/c", "/bin/c extra")],
        )
        self.assertEqual(parse_execstart(""), [])
        self.assertEqual(parse_execstart("no braces"), [])

    def test_uses_argv_when_path_is_empty(self):
        text = "{ path= ; argv[]=/usr/bin/foo --bar ; ignore_errors=no }"
        self.assertEqual(parse_execstart(text), [("/usr/bin/foo", "/usr/bin/foo --bar")])


class DesktopRuleTests(unittest.TestCase):
    def test_desktop_visibility(self):
        gnome = {"ubuntu", "GNOME"}
        self.assertTrue(desktop_visible("GNOME;Unity;", "", gnome))
        self.assertTrue(desktop_visible("ubuntu;", "", {"ubuntu", "GNOME"}))
        self.assertFalse(desktop_visible("KDE;", "", {"GNOME"}))
        self.assertFalse(desktop_visible("GNOME;", "", set()))
        self.assertFalse(desktop_visible("", "GNOME;", {"GNOME"}))
        self.assertFalse(desktop_visible("", "ubuntu;", {"ubuntu", "GNOME"}))
        self.assertTrue(desktop_visible("", "KDE;", {"GNOME"}))
        self.assertTrue(desktop_visible("", "", {"GNOME"}))
        self.assertTrue(desktop_visible(None, None, set()))

    def test_autostart_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker"
            marker.write_text("x", encoding="utf-8")
            self.assertTrue(autostart_condition_allows(None))
            self.assertTrue(autostart_condition_allows("  "))
            self.assertTrue(autostart_condition_allows(f"if-exists {marker}"))
            self.assertFalse(autostart_condition_allows("if-exists /no/such/marker"))
            self.assertFalse(autostart_condition_allows(f"unless-exists {marker}"))
            self.assertTrue(autostart_condition_allows("unless-exists /no/such/marker"))

        self.assertTrue(
            autostart_condition_allows(
                "GSettings org.example enabled",
                settings_lookup=lambda _schema, _key: True,
            )
        )
        self.assertFalse(
            autostart_condition_allows(
                "GSettings org.example enabled",
                settings_lookup=lambda _schema, _key: False,
            )
        )
        def unavailable(_schema: str, _key: str) -> bool:
            raise LookupError("unavailable")

        self.assertTrue(
            autostart_condition_allows(
                "GSettings org.example enabled",
                settings_lookup=unavailable,
            )
        )
        self.assertTrue(autostart_condition_allows("GSettings only-schema"))
        self.assertTrue(autostart_condition_allows("something-else"))
        self.assertTrue(
            autostart_condition_allows("GSettings org.example.does.not.exist key")
        )

    def test_entry_activity_and_override(self):
        current = {"GNOME"}
        self.assertTrue(entry_is_active(record(), current))
        self.assertFalse(entry_is_active(record(hidden=True), current))
        self.assertFalse(entry_is_active(record(gnome_enabled=False), current))
        self.assertFalse(entry_is_active(record(entry_type="Link"), current))
        self.assertTrue(entry_is_active(record(entry_type=""), current))
        self.assertFalse(
            entry_is_active(
                record(try_exec="/missing"),
                current,
                program_exists_fn=lambda _program: False,
            )
        )
        self.assertFalse(entry_is_active(record(only_show_in="KDE;"), current))

        selected = select_desktop_records(
            [
                record(filename="a.desktop", name="User", group=GROUP_USER),
                record(
                    filename="a.desktop",
                    name="System",
                    group=GROUP_SYSTEM,
                    path="/etc/a.desktop",
                ),
                record(filename="b.desktop", name="Other", group=GROUP_SYSTEM),
            ],
            current,
        )
        self.assertEqual([item.name for item in selected], ["User", "Other"])

        suppressed = select_desktop_records(
            [
                record(filename="a.desktop", hidden=True, name="Hidden user"),
                record(filename="a.desktop", name="System copy", group=GROUP_SYSTEM),
            ],
            current,
        )
        self.assertEqual([item.name for item in suppressed], ["Hidden user"])

        claimed_by_disabled = select_desktop_records(
            [
                record(
                    filename="a.desktop",
                    gnome_enabled=False,
                    name="Disabled user",
                ),
                record(filename="a.desktop", name="System copy", group=GROUP_SYSTEM),
            ],
            current,
        )
        self.assertEqual([item.name for item in claimed_by_disabled], ["Disabled user"])

    def test_display_path_and_search(self):
        home = str(Path.home())
        self.assertEqual(display_path(f"{home}/.config/autostart"), "~/.config/autostart")
        self.assertEqual(display_path(home), "~")
        self.assertEqual(display_path("/etc/xdg/autostart"), "/etc/xdg/autostart")
        self.assertEqual(display_path(f"{home}-other/file"), f"{home}-other/file")

        entry = record_to_entry(record(name="NetBird", exec_line="/usr/bin/netbird-ui"))
        self.assertTrue(entry_matches(entry, "netbird"))
        self.assertTrue(entry_matches(entry, "/usr/bin/netbird-ui"))
        self.assertTrue(entry_matches(entry, "app.desktop"))
        self.assertFalse(entry_matches(entry, "dropbox"))


class DesktopScanTests(unittest.TestCase):
    def test_scan_reads_filters_and_preserves_directory_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = root / "user"
            system = root / "system"
            gnome = root / "gnome"
            for directory in (user, system, gnome):
                directory.mkdir()
            marker = root / "marker"
            marker.write_text("x", encoding="utf-8")

            (user / "keep.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Keep\n"
                "Comment=User app\nExec=/usr/bin/keep\nIcon=keep\n",
                encoding="utf-8",
            )
            (user / "link.desktop").symlink_to(user / "keep.desktop")
            (user / "hidden.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Hidden User\n"
                "Exec=/usr/bin/hidden\nHidden=true\n",
                encoding="utf-8",
            )
            (user / "broken.desktop").write_text("not a desktop file", encoding="utf-8")
            (user / "notes.txt").write_text("ignore", encoding="utf-8")
            nested = user / "nested"
            nested.mkdir()
            (nested / "nope.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Nested\nExec=/bin/true\n",
                encoding="utf-8",
            )

            (system / "hidden.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Hidden System\n"
                "Exec=/usr/bin/hidden-system\n",
                encoding="utf-8",
            )
            (system / "broken.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Broken System\n"
                "Exec=/usr/bin/broken\n",
                encoding="utf-8",
            )
            (system / "kde-only.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=KDE Only\n"
                "Exec=/usr/bin/kde\nOnlyShowIn=KDE;\n",
                encoding="utf-8",
            )
            (system / "not-here.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Not Here\n"
                "Exec=/usr/bin/not-here\nNotShowIn=GNOME;\n",
                encoding="utf-8",
            )
            (system / "fields.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Fields\n"
                "Exec=/usr/bin/fields %U\nOnlyShowIn=GNOME;\n",
                encoding="utf-8",
            )
            (system / "from-env.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=From Env\n"
                "Exec=env EXAMPLE=1 /usr/bin/from-env\n",
                encoding="utf-8",
            )
            (system / "try-ok.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Try Ok\n"
                "Exec=/usr/bin/true\nTryExec=/usr/bin/true\n",
                encoding="utf-8",
            )

            (gnome / "disabled.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Disabled\n"
                "Exec=/usr/bin/disabled\nX-GNOME-Autostart-enabled=false\n",
                encoding="utf-8",
            )
            (gnome / "conditioned.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Conditioned\n"
                "Exec=/usr/bin/conditioned\n"
                "AutostartCondition=GSettings org.example off\n",
                encoding="utf-8",
            )
            (gnome / "enabled-by-setting.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Enabled By Setting\n"
                "Exec=/usr/bin/enabled-by-setting\n"
                "AutostartCondition=GSettings org.example on\n",
                encoding="utf-8",
            )
            (gnome / "missing-try.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Missing Try\n"
                "Exec=/usr/bin/missing\n"
                "TryExec=/this/does/not/exist/startup-applications-test\n",
                encoding="utf-8",
            )
            (gnome / "exists.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Exists\n"
                f"Exec=/usr/bin/exists\nAutostartCondition=if-exists {marker}\n",
                encoding="utf-8",
            )
            (gnome / "missing-file.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Missing File\n"
                "Exec=/usr/bin/missing-file\n"
                "AutostartCondition=if-exists /no/such/startup-applications-marker\n",
                encoding="utf-8",
            )
            (gnome / "dbus.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Bus Only\n"
                "DBusActivatable=true\n",
                encoding="utf-8",
            )

            entries = scan_desktop(
                [(GROUP_USER, user), (GROUP_SYSTEM, system), (GROUP_GNOME, gnome)],
                current_desktops_set={"GNOME"},
                settings_lookup=lambda _schema, key: key == "on",
                program_exists_fn=lambda program: program == "/usr/bin/true",
            )

        self.assertEqual(
            [entry.name for entry in entries],
            [
                "Hidden User",
                "Keep",
                "Keep",
                "Fields",
                "From Env",
                "Try Ok",
                "Bus Only",
                "Disabled",
                "Enabled By Setting",
                "Exists",
            ],
        )
        self.assertEqual(
            [entry.group for entry in entries],
            [GROUP_USER, GROUP_USER, GROUP_USER, GROUP_SYSTEM, GROUP_SYSTEM, GROUP_SYSTEM]
            + [GROUP_GNOME, GROUP_GNOME, GROUP_GNOME, GROUP_GNOME],
        )
        hidden = next(entry for entry in entries if entry.name == "Hidden User")
        disabled = next(entry for entry in entries if entry.name == "Disabled")
        self.assertFalse(hidden.enabled)
        self.assertFalse(disabled.enabled)
        self.assertTrue(next(entry for entry in entries if entry.name == "Keep").enabled)
        link = next(entry for entry in entries if entry.source_path.endswith("link.desktop"))
        self.assertTrue(link.symlink_target.endswith("keep.desktop"))
        fields = next(entry for entry in entries if entry.name == "Fields")
        self.assertEqual(fields.executable, "/usr/bin/fields")
        self.assertEqual(fields.command_line, "/usr/bin/fields %U")
        from_env = next(entry for entry in entries if entry.name == "From Env")
        self.assertEqual(from_env.executable, "/usr/bin/from-env")
        bus_only = next(entry for entry in entries if entry.name == "Bus Only")
        self.assertEqual(bus_only.executable, dbus_activated_label(""))
        kept = next(entry for entry in entries if entry.source_path.endswith("keep.desktop"))
        self.assertEqual(kept.comment, "User app")
        self.assertEqual(kept.icon_name, "keep")

    def test_groups_omit_empty_sources_and_sort_names(self):
        entries = [
            record_to_entry(record(name="Zed", group=GROUP_USER, filename="z.desktop")),
            record_to_entry(record(name="alpha", group=GROUP_USER, filename="a.desktop")),
            record_to_entry(
                record(name="System", group=GROUP_SYSTEMD_SYSTEM, filename="s.desktop")
            ),
        ]
        groups = build_groups(
            entries,
            {GROUP_USER: "~/.config/autostart", GROUP_SYSTEMD_SYSTEM: "systemd system"},
        )
        self.assertEqual([group.key for group in groups], [GROUP_USER, GROUP_SYSTEMD_SYSTEM])
        self.assertEqual([entry.name for entry in groups[0].entries], ["alpha", "Zed"])
        self.assertEqual(groups[0].description, "~/.config/autostart")
        self.assertEqual(groups[1].description, "systemd system")


class SystemdParseTests(unittest.TestCase):
    def test_unit_list_and_show_output(self):
        units = parse_unit_file_list(
            "foo.service  enabled enabled\n"
            "bar.service  enabled-runtime enabled\n"
            "not-a-unit\n"
            "\n"
        )
        self.assertEqual(units, ["foo.service", "bar.service"])

        text = """
Id=foo.service
Description=Foo Service
FragmentPath=/usr/lib/systemd/system/foo.service
UnitFileState=enabled
BusName=org.foo
ExecStart={ path=/usr/bin/foo ; argv[]=/usr/bin/foo --serve ; ignore_errors=no ; pid=1 }

Id=foo-alias.service
Description=Foo Alias
FragmentPath=/usr/lib/systemd/system/foo.service
UnitFileState=enabled
BusName=org.foo
ExecStart={ path=/usr/bin/foo ; argv[]=/usr/bin/foo --serve ; ignore_errors=no }

Id=bar.service
Description=Bar
FragmentPath=/usr/lib/systemd/system/bar.service
UnitFileState=enabled
BusName=org.bar

Id=empty.service
Description=Empty
FragmentPath=/usr/lib/systemd/system/empty.service
UnitFileState=enabled
BusName=
"""
        records = parse_systemctl_show(text)
        self.assertEqual(
            [record["Id"] for record in records],
            ["foo.service", "foo-alias.service", "bar.service", "empty.service"],
        )
        entries = entries_from_unit_records(
            text,
            GROUP_SYSTEMD_SYSTEM,
            icon_for_unit=lambda _unit: ("", ""),
        )
        self.assertEqual([entry.name for entry in entries], ["Foo Service", "Bar", "Empty"])
        foo, bar, empty = entries
        self.assertEqual(foo.executable, "/usr/bin/foo")
        self.assertEqual(foo.command_line, "/usr/bin/foo --serve")
        self.assertEqual(foo.source_path, "/usr/lib/systemd/system/foo.service")
        self.assertEqual(foo.unit_id, "foo.service")
        self.assertEqual(bar.executable, dbus_activated_label("org.bar"))
        self.assertEqual(empty.executable, NO_EXECUTABLE)
        self.assertEqual(empty.command_line, "")

    def test_multiple_exec_starts_are_kept(self):
        text = """
Id=multi.service
Description=Multi
FragmentPath=/usr/lib/systemd/system/multi.service
UnitFileState=enabled
BusName=
ExecStart={ path=/bin/b ; argv[]=/bin/b ; ignore_errors=no }
ExecStart={ path=/bin/c ; argv[]=/bin/c extra ; ignore_errors=no }
"""
        entries = entries_from_unit_records(
            text,
            GROUP_SYSTEMD_SYSTEM,
            icon_for_unit=lambda _unit: ("service-icon", ""),
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].executable, "/bin/b")
        self.assertEqual(entries[0].command_line, "/bin/b\n/bin/c extra")
        self.assertEqual(entries[0].icon_name, "service-icon")

    def test_relative_execstart_path_is_resolved(self):
        text = """
Id=shell.service
Description=Shell
FragmentPath=/usr/lib/systemd/system/shell.service
UnitFileState=enabled
BusName=
ExecStart={ path=sh ; argv[]=sh -c true ; ignore_errors=no }
"""
        entries = entries_from_unit_records(
            text,
            GROUP_SYSTEMD_SYSTEM,
            icon_for_unit=lambda _unit: ("", ""),
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue(os.path.isabs(entries[0].executable))
        self.assertTrue(entries[0].executable.endswith("/sh"))
        self.assertEqual(entries[0].command_line, "sh -c true")

    def test_template_units_are_read_from_the_unit_file(self):
        self.assertTrue(is_template_unit("getty@.service"))
        self.assertFalse(is_template_unit("getty@tty1.service"))
        self.assertFalse(is_template_unit("accounts-daemon.service"))
        text = """# /usr/lib/systemd/system/getty@.service
[Unit]
Description=Getty on %I

[Service]
ExecStart=-/usr/sbin/agetty --noreset --noclear - ${TERM}
ExecStart=
ExecStart=/usr/bin/true
"""
        fragment, description, commands = parse_systemctl_cat(text)
        self.assertEqual(fragment, "/usr/lib/systemd/system/getty@.service")
        self.assertEqual(description, "Getty on %I")
        self.assertEqual(commands, ["/usr/bin/true"])

        entry = entry_from_unit_file(
            """# /usr/lib/systemd/system/getty@.service
[Unit]
Description=Getty on %I

[Service]
ExecStart=-/usr/sbin/agetty --noreset --noclear - ${TERM}
""",
            "getty@.service",
            GROUP_SYSTEMD_SYSTEM,
            icon_for_unit=lambda _unit: ("", ""),
        )
        assert entry is not None
        self.assertEqual(entry.name, "Getty on %I")
        self.assertEqual(entry.executable, "/usr/sbin/agetty")
        self.assertEqual(entry.command_line, "/usr/sbin/agetty --noreset --noclear - ${TERM}")
        self.assertEqual(entry.source_path, "/usr/lib/systemd/system/getty@.service")


def _desktop_flag(path: Path, key: str) -> bool:
    from gi.repository import GLib

    keyfile = GLib.KeyFile()
    keyfile.load_from_file(str(path), GLib.KeyFileFlags.NONE)
    return bool(keyfile.get_boolean("Desktop Entry", key))


class AutostartChangeTests(unittest.TestCase):
    def test_desktop_writer_copies_system_file_and_replaces_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            system = root / "system"
            user = root / "user"
            system.mkdir()
            user.mkdir()
            source = system / "app.desktop"
            source.write_text(
                "[Desktop Entry]\nType=Application\nName=App\nExec=/usr/bin/app\n",
                encoding="utf-8",
            )
            written = Path(set_desktop_autostart(str(source), False, user_directory=user))
            self.assertEqual(written, user / "app.desktop")
            self.assertTrue(_desktop_flag(written, "Hidden"))
            self.assertFalse(_desktop_flag(written, "X-GNOME-Autostart-enabled"))
            self.assertNotIn("Hidden", source.read_text(encoding="utf-8"))

            enabled = Path(set_desktop_autostart(str(written), True, user_directory=user))
            self.assertFalse(_desktop_flag(enabled, "Hidden"))
            self.assertTrue(_desktop_flag(enabled, "X-GNOME-Autostart-enabled"))

            target = system / "linked.desktop"
            target.write_text(
                "[Desktop Entry]\nType=Application\nName=Linked\nExec=/usr/bin/linked\n",
                encoding="utf-8",
            )
            original = target.read_text(encoding="utf-8")
            link = user / "linked.desktop"
            link.symlink_to(target)
            set_desktop_autostart(str(link), False, user_directory=user)
            self.assertFalse(link.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertTrue(_desktop_flag(link, "Hidden"))

    def test_disabled_unit_config_remembers_and_forgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "disabled-units.conf"
            remember_disabled_unit("system", "foo.service", path=path)
            remember_disabled_unit("user", "bar.service", path=path)
            stored = read_disabled_units(path)
            self.assertEqual(stored["system"], ["foo.service"])
            self.assertEqual(stored["user"], ["bar.service"])
            forget_disabled_unit("system", "foo.service", path=path)
            stored = read_disabled_units(path)
            self.assertEqual(stored["system"], [])
            self.assertEqual(stored["user"], ["bar.service"])
            forget_disabled_unit("user", "bar.service", path=path)
            self.assertFalse(path.exists())


def _container(
    name: str,
    *,
    policy: str,
    status: str,
    image: str = "nginx",
    project: str = "",
    service: str = "",
    workdir: str = "",
    config_files: str = "",
) -> dict:
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
    if service:
        labels["com.docker.compose.service"] = service
    if workdir:
        labels["com.docker.compose.project.working_dir"] = workdir
    if config_files:
        labels["com.docker.compose.project.config_files"] = config_files
    return {
        "Name": f"/{name}",
        "Config": {"Image": image, "Labels": labels},
        "HostConfig": {"RestartPolicy": {"Name": policy}},
        "State": {"Status": status},
    }


class DockerInspectTests(unittest.TestCase):
    def test_restart_policy_rules(self):
        self.assertTrue(container_autostarts("always", "exited"))
        self.assertTrue(container_autostarts("always", "running"))
        self.assertTrue(container_autostarts("unless-stopped", "running"))
        self.assertTrue(container_autostarts("unless-stopped", "paused"))
        self.assertTrue(container_autostarts("unless-stopped", "restarting"))
        self.assertFalse(container_autostarts("unless-stopped", "exited"))
        self.assertFalse(container_autostarts("unless-stopped", "created"))
        self.assertFalse(container_autostarts("no", "running"))
        self.assertFalse(container_autostarts("on-failure", "running"))

    def test_groups_compose_project_and_keeps_standalone_container(self):
        entries = entries_from_docker_inspect(
            [
                _container(
                    "speaker-play-client",
                    policy="unless-stopped",
                    status="running",
                    project="speaker-play",
                    service="speaker-play-client",
                    workdir="/srv/speaker-play",
                    config_files="/srv/speaker-play/compose.yaml,/srv/speaker-play/extra.yaml",
                ),
                _container(
                    "speaker-play-relay",
                    policy="unless-stopped",
                    status="running",
                    project="speaker-play",
                    service="speaker-play-relay",
                    workdir="/srv/speaker-play",
                    config_files="/srv/speaker-play/compose.yaml",
                ),
                _container(
                    "speaker-play-server",
                    policy="unless-stopped",
                    status="exited",
                    project="speaker-play",
                    service="speaker-play-server",
                    workdir="/srv/speaker-play",
                    config_files="/srv/speaker-play/compose.yaml",
                ),
                _container(
                    "dcbuild-server",
                    policy="no",
                    status="exited",
                    project="dc-build",
                    service="dcbuild-server",
                    workdir="/srv/dc-build",
                    config_files="/srv/dc-build/docker-compose.yml",
                ),
                _container(
                    "urlshort",
                    policy="always",
                    status="exited",
                    project="martinsz-urlshort",
                    service="urlshort",
                    workdir="/srv/urlshort",
                    config_files="/srv/urlshort/compose.yaml",
                ),
                _container(
                    "cloudflared",
                    policy="unless-stopped",
                    status="running",
                    image="cloudflare/cloudflared",
                ),
            ]
        )
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"speaker-play", "martinsz-urlshort", "cloudflared"})

        project = by_name["speaker-play"]
        self.assertEqual(project.group, GROUP_DOCKER)
        self.assertEqual(project.executable, "/srv/speaker-play")
        self.assertEqual(project.source_path, "/srv/speaker-play/compose.yaml")
        self.assertEqual(
            project.comment,
            "speaker-play-client: unless-stopped, running\n"
            "speaker-play-relay: unless-stopped, running",
        )
        self.assertTrue(project.enabled)
        self.assertTrue(project.icon_name.endswith("docker-icon.png"))

        stopped_always = by_name["martinsz-urlshort"]
        self.assertEqual(stopped_always.comment, "urlshort: always, exited")

        standalone = by_name["cloudflared"]
        self.assertEqual(standalone.executable, "cloudflare/cloudflared")
        self.assertEqual(standalone.source_path, "Docker container")
        self.assertEqual(standalone.comment, "unless-stopped, running")


class AddAutostartTests(unittest.TestCase):
    def test_creates_desktop_file_and_does_not_overwrite(self):
        from gi.repository import GLib

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = Path(
                add_user_autostart(
                    "AnyDesk Tray",
                    "/usr/bin/anydesk --tray",
                    "anydesk",
                    user_directory=directory,
                )
            )
            second = Path(
                add_user_autostart(
                    "AnyDesk Tray",
                    "/usr/bin/anydesk",
                    user_directory=directory,
                )
            )
            self.assertEqual(first.name, "anydesk-tray.desktop")
            self.assertEqual(second.name, "anydesk-tray-2.desktop")
            keyfile = GLib.KeyFile()
            keyfile.load_from_file(str(first), GLib.KeyFileFlags.NONE)
            self.assertEqual(keyfile.get_string("Desktop Entry", "Type"), "Application")
            self.assertEqual(keyfile.get_string("Desktop Entry", "Name"), "AnyDesk Tray")
            self.assertEqual(keyfile.get_string("Desktop Entry", "Exec"), "/usr/bin/anydesk --tray")
            self.assertEqual(keyfile.get_string("Desktop Entry", "Icon"), "anydesk")
            self.assertFalse(keyfile.get_boolean("Desktop Entry", "Hidden"))
            self.assertTrue(keyfile.get_boolean("Desktop Entry", "X-GNOME-Autostart-enabled"))

            plain = GLib.KeyFile()
            plain.load_from_file(str(second), GLib.KeyFileFlags.NONE)
            keys = plain.get_keys("Desktop Entry")[0]
            self.assertNotIn("Icon", list(keys))
            self.assertEqual(plain.get_string("Desktop Entry", "Exec"), "/usr/bin/anydesk")
            self.assertEqual(keyfile.get_string("Desktop Entry", "Exec"), "/usr/bin/anydesk --tray")


class FlatpakPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("FLATPAK_ID")
        os.environ["FLATPAK_ID"] = "io.github.martinszeltins.Awaken"

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("FLATPAK_ID", None)
        else:
            os.environ["FLATPAK_ID"] = self._saved

    def test_translates_mounts_to_the_paths_the_native_app_shows(self) -> None:
        home = str(Path.home())
        sandbox = (
            f"{home}/.var/app/io.github.martinszeltins.Awaken/config/autostart/netbird.desktop"
        )
        host = f"{home}/.config/autostart/netbird.desktop"
        self.assertEqual(to_host_path(sandbox), host)
        self.assertEqual(to_sandbox_path(host), sandbox)
        self.assertEqual(display_path(to_host_path(sandbox)), "~/.config/autostart/netbird.desktop")
        self.assertEqual(
            to_host_path("/run/host/etc/xdg/autostart/user-dirs-update-gtk.desktop"),
            "/etc/xdg/autostart/user-dirs-update-gtk.desktop",
        )
        self.assertEqual(
            to_sandbox_path("/etc/xdg/autostart/user-dirs-update-gtk.desktop"),
            "/run/host/etc/xdg/autostart/user-dirs-update-gtk.desktop",
        )
        self.assertEqual(
            to_host_path("/run/host/usr/share/gnome/autostart/gnome-login-sound.desktop"),
            "/usr/share/gnome/autostart/gnome-login-sound.desktop",
        )
        self.assertEqual(to_host_path("/usr/bin/anydesk"), "/usr/bin/anydesk")

    def test_system_autostart_is_read_from_the_host_mounts(self) -> None:
        groups = dict(autostart_directories())
        self.assertEqual(groups[GROUP_SYSTEM], Path("/run/host/etc/xdg/autostart"))
        self.assertEqual(groups[GROUP_GNOME], Path("/run/host/usr/share/gnome/autostart"))

    def test_desktop_entry_stores_the_host_path(self) -> None:
        entry = record_to_entry(
            record(path="/run/host/usr/share/gnome/autostart/login.desktop", group=GROUP_GNOME)
        )
        self.assertEqual(entry.source_path, "/usr/share/gnome/autostart/login.desktop")

    def test_compose_menu_uses_absolute_host_paths(self) -> None:
        directory, compose = docker_locations_visible(
            "/home/martins/Programming/ntfy",
            "/home/martins/Programming/ntfy/docker-compose.yaml",
        )
        self.assertTrue(directory)
        self.assertTrue(compose)
        image, standalone = docker_locations_visible(
            "cloudflare/cloudflared:latest",
            "Docker container",
        )
        self.assertFalse(image)
        self.assertFalse(standalone)

    def test_unreadable_host_files_stay_in_the_menu(self) -> None:
        self.assertTrue(host_file_visible("/home/martins/.config/systemd/user/app.service"))
        self.assertFalse(host_file_visible("Docker container"))
        with patch("startup_applications.scanner.os.path.exists", return_value=True):
            self.assertEqual(
                readable_path("/etc/xdg/autostart/user-dirs-update-gtk.desktop"),
                "/run/host/etc/xdg/autostart/user-dirs-update-gtk.desktop",
            )


if __name__ == "__main__":
    unittest.main()
