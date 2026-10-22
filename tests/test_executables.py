from contextlib import nullcontext
from functools import partial
import io
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

from butler.cli import main
from butler.client import run_session
from butler.config import Settings
from butler.errors import ButlerError, ConfigError
from butler.protocol import decode, encode
from butler.runtime import resolve_executable
from tests.support import FAST, ROOT, RUNTIME, WorkspaceCase


class ExecutableResolutionTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.binary = self.work / "custom nvim ' $HOME;$(touch injected)"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o755)

    def test_path_lookup_absolute_relative_and_home_paths(self):
        with patch.dict(os.environ, {"HOME": str(self.work), "PATH": str(self.work)}):
            for value in (self.binary.name, str(self.binary), os.path.relpath(self.binary),
                          "~/" + self.binary.name):
                with self.subTest(value=value):
                    self.assertEqual(resolve_executable(value, "Local Neovim"), str(self.binary))

    def test_relative_path_entry_becomes_absolute(self):
        with patch.dict(os.environ, {"PATH": os.path.relpath(self.work)}):
            self.assertEqual(resolve_executable(self.binary.name, "Remote Neovim"), str(self.binary))

    def test_missing_nonexecutable_and_directory_errors_identify_the_choice(self):
        self.binary.chmod(0o644)
        for value in (str(self.binary), str(self.work / "missing"), str(self.work)):
            with self.subTest(value=value), self.assertRaises(ButlerError) as error:
                resolve_executable(value, "Remote Neovim")
            self.assertIn("Remote Neovim", str(error.exception))
            self.assertIn(repr(value), str(error.exception))

    def test_malformed_values_are_configuration_errors(self):
        for value in (None, False, 123, [], "", "bad\0path"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                resolve_executable(value, "Local Neovim")


class ExecutableCommandTests(WorkspaceCase):
    def test_cli_defaults_and_independent_overrides(self):
        choices = (
            ([], "nvim", "nvim"),
            (["--nvim", "./local nvim"], "./local nvim", "nvim"),
            (["--remote-nvim", "~/remote nvim"], "nvim", "~/remote nvim"),
            (["--nvim", "local-nvim", "--remote-nvim", "remote-nvim"], "local-nvim", "remote-nvim"),
        )
        with patch.dict(os.environ, self.environment(), clear=True):
            for command in ("connect", "create"):
                positional = ["~/project"] if command == "create" else []
                for flags, nvim, remote_nvim in choices:
                    with self.subTest(command=command, flags=flags), patch(f"butler.cli.{command}") as action:
                        self.assertEqual(main([command, *positional, *flags]), 0)
                        action.assert_called_once_with(Settings(), *positional, nvim=nvim, remote_nvim=remote_nvim)

    def test_invalid_local_executable_fails_before_discovery_or_session_creation(self):
        env = self.fake_environment()
        blocked = self.work / "not-executable"
        blocked.touch(mode=0o644)
        before = set(RUNTIME.iterdir())
        with patch.dict(os.environ, env, clear=True), patch("butler.client.terminal_state", nullcontext):
            for value, status in ((str(self.work / "missing"), 1), (str(blocked), 1), ("", 2), ("bad\0path", 2)):
                for command in ("connect", "create"):
                    arguments = [command, str(self.work / "new-project")] if command == "create" else [command]
                    with self.subTest(command=command, value=value), patch("sys.stderr", new_callable=io.StringIO) as error:
                        self.assertEqual(main([*arguments, "--nvim", value]), status)
                    self.assertIn("Local Neovim", error.getvalue())
        self.assertEqual(self.events(env), [])
        self.assertEqual(set(RUNTIME.iterdir()), before)
        self.assertFalse((self.work / "new-project").exists())


class ExecutableSessionTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.env = self.fake_environment()
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.local = self.work / "local nvim ' $HOME;$(touch injected)"
        self.remote = self.work / "remote nvim ' $HOME;$(touch injected)"
        fixture = ROOT / "tests" / "fixtures" / "fake_nvim.py"
        for binary in (self.local, self.remote):
            binary.write_text(f"#!{sys.executable}\n" + fixture.read_text())
            binary.chmod(0o755)
        self.default = str(fixture)

    def assert_clean(self, events):
        for event in events:
            self.assert_gone(event["pid"])
            if "args" in event:
                args = event["args"]
                option = "--listen" if "--listen" in args else "--server"
                self.assertFalse(Path(args[args.index(option) + 1]).parent.exists())

    def test_both_commands_route_independent_overrides_to_the_correct_processes(self):
        session = partial(run_session, runtime_root=RUNTIME, remote_runtime_root=RUNTIME, timings=FAST)
        choices = (
            (["--nvim", os.path.relpath(self.local)], str(self.local), self.default),
            (["--remote-nvim", "./" + self.remote.name], self.default, str(self.remote)),
            (["--nvim", "~/" + self.local.name, "--remote-nvim", "~/" + self.remote.name],
             str(self.local), str(self.remote)),
        )
        with patch.dict(os.environ, self.env, clear=True), patch("butler.client.terminal_state", nullcontext), \
                patch("butler.client.run_session", session):
            for command in ("connect", "create"):
                for index, (flags, local, remote) in enumerate(choices):
                    project = self.work / f"{command}-{index}"
                    if command == "connect":
                        project.mkdir()
                        arguments = [command, "--root", str(project), "--depth", "0"]
                    else:
                        arguments = [command, str(project)]
                    start = len(self.events(self.env))
                    with self.subTest(command=command, flags=flags):
                        self.assertEqual(main([*arguments, *flags]), 0)
                        events = self.events(self.env)[start:]
                        for role, expected in (("ui", local), ("probe", local), ("server", remote)):
                            actual = [event["executable"] for event in events if event["role"] == role]
                            self.assertTrue(actual, role)
                            self.assertEqual(set(actual), {expected}, role)
                        self.assertEqual((self.work / "selected-cwd").read_bytes(), os.fsencode(project) + b"\n")
                        if command == "create":
                            self.assertTrue((project / ".git").is_dir())
                        self.assert_clean(events)
        self.assertFalse((self.work / "injected").exists())
        self.assertFalse((ROOT / "injected").exists())

    def test_custom_binaries_work_without_nvim_on_path(self):
        (self.work / "bin" / "nvim").unlink()
        (self.work / "bin" / "python3").symlink_to(sys.executable)
        self.env["PATH"] = str(self.work / "bin")
        with patch.dict(os.environ, self.env, clear=True):
            run_session(Settings(), str(self.work), nvim=str(self.local), remote_nvim=str(self.remote),
                        runtime_root=RUNTIME, remote_runtime_root=RUNTIME, timings=FAST)
        self.assert_clean(self.events(self.env))

    def test_bad_remote_override_cleans_client_and_does_not_create_repository(self):
        destination = self.work / "new-parent" / "project"
        before = set(RUNTIME.iterdir())
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(ButlerError, "Remote Neovim"):
                run_session(Settings(), str(destination), create=True, remote_nvim="~/missing",
                            runtime_root=RUNTIME, remote_runtime_root=RUNTIME, timings=FAST)
        self.assertFalse(destination.parent.exists())
        self.assertEqual(set(RUNTIME.iterdir()), before)
        self.assert_clean(self.events(self.env))
        self.assertFalse(any(event["role"] in {"server", "ui"} for event in self.events(self.env)))

    def test_remote_helper_rejects_invalid_overrides_before_creating_resources(self):
        blocked = self.work / "not-executable"
        blocked.touch(mode=0o644)
        destination = self.work / "new-parent" / "project"
        before = set(RUNTIME.iterdir())
        for value in (None, False, 123, [], "", "bad\0path", "~/missing", str(blocked)):
            with self.subTest(value=value):
                proc = self.launch_server(self.env)
                stdout, stderr = proc.communicate(
                    encode("start", session=uuid.uuid4().hex, directory=str(destination), create=True, nvim=value),
                    timeout=5,
                )
                self.assertNotEqual(proc.returncode, 0, stderr.decode())
                error = decode(stdout)
                self.assertEqual(error["type"], "error")
                self.assertIn("Remote Neovim", error["message"])
                self.assertFalse(destination.parent.exists())
                self.assertEqual(set(RUNTIME.iterdir()), before)
        self.assertEqual(self.events(self.env), [])
