import io
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import termios
import time
import uuid
from unittest.mock import patch

from butler.cli import main
from butler.client import run_session
from butler.config import Settings
from butler.errors import ButlerError
from butler.protocol import decode, encode, send
from tests.support import RUNTIME, FAST, ROOT, WorkspaceCase


class CreateCommandTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.config = self.work / "profiles.toml"
        self.config.write_text('''[profiles.default]
host = "default-host"
root = "~/ignored"
depth = 5
[profiles.work]
host = "work-host"
''')

    def test_profile_selection_and_host_override(self):
        for arguments, host in (([], "default-host"), (["work"], "work-host"),
                                (["work", "--host", "override"], "override")):
            with self.subTest(arguments=arguments), patch("butler.cli.create") as action:
                self.assertEqual(main(["create", "~/dev/foo", *arguments,
                                       "--config", str(self.config)]), 0)
                action.assert_called_once_with(Settings(host, "~/ignored", 5), "~/dev/foo",
                                               nvim="nvim", remote_nvim="nvim")

    def test_connect_arguments_still_work(self):
        with patch("butler.cli.connect") as action:
            self.assertEqual(main(["connect", "work", "--host", "other", "--root", "~/projects",
                                   "--depth", "0", "--config", str(self.config)]), 0)
            action.assert_called_once_with(Settings("other", "~/projects", 0),
                                           nvim="nvim", remote_nvim="nvim")

    def test_invalid_destinations_fail_before_opening_a_session(self):
        for destination in ("", "relative/foo", "~someone/foo", "/invalid\0path"):
            with self.subTest(destination=destination), patch("butler.client.run_session") as session:
                with patch("sys.stderr", new_callable=io.StringIO) as error:
                    self.assertEqual(main(["create", destination, "--config", str(self.config)]), 2)
                self.assertIn("Destination", error.getvalue())
                session.assert_not_called()

    def test_requires_an_interactive_terminal(self):
        with patch("sys.stdin.isatty", return_value=False), patch("butler.client.run_session") as session:
            with patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(main(["create", "~/foo", "--config", str(self.config)]), 1)
            self.assertIn("interactive terminal", error.getvalue())
            session.assert_not_called()


class CreateRepositoryTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.env = self.fake_environment()
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = str(self.work / ".gitconfig")
        self.destination = self.work / "new-parent" / "project"

    def helper_request(self, **fields):
        proc = self.launch_server(self.env)
        stdout, stderr = proc.communicate(
            encode("start", session=uuid.uuid4().hex, directory=str(self.destination), **fields), timeout=8,
        )
        messages = [decode(line) for line in stdout.splitlines()]
        return proc.returncode, messages, stderr.decode(errors="replace")

    def install_git_stub(self, mode="failure"):
        script = self.work / "bin" / "git"
        fixture = ROOT / "tests" / "fixtures" / "fake_git.py"
        script.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(fixture)!r}, run_name='__main__')\n")
        script.chmod(0o755)
        self.env["TEST_GIT_MODE"] = mode

    def assert_no_editor(self):
        self.assertFalse(any(event["role"] in {"ui", "server"} for event in self.events(self.env)))

    def test_existing_destinations_are_untouched(self):
        for kind in ("empty", "nonempty", "repository", "file", "symlink", "broken-symlink"):
            self.destination = self.work / kind
            if kind == "file":
                self.destination.write_text("keep me")
            elif kind in {"symlink", "broken-symlink"}:
                self.destination.symlink_to(self.work if kind == "symlink" else self.work / "missing")
            else:
                self.destination.mkdir()
                if kind == "nonempty":
                    (self.destination / "keep").write_text("keep me")
                elif kind == "repository":
                    subprocess.run(["git", "init", str(self.destination)], env=self.env,
                                   capture_output=True, check=True)
                    original_head = (self.destination / ".git" / "HEAD").read_bytes()
            with self.subTest(kind=kind):
                status, messages, stderr = self.helper_request(create=True)
                self.assertEqual(status, 1, stderr)
                self.assertIn("already exists", messages[0]["message"])
                if kind in {"empty", "nonempty"}:
                    self.assertTrue(self.destination.is_dir())
                    self.assertFalse((self.destination / ".git").exists())
                if kind == "nonempty":
                    self.assertEqual((self.destination / "keep").read_text(), "keep me")
                if kind == "file":
                    self.assertEqual(self.destination.read_text(), "keep me")
                if kind == "repository":
                    self.assertEqual((self.destination / ".git" / "HEAD").read_bytes(), original_head)
                if kind in {"symlink", "broken-symlink"}:
                    self.assertTrue(self.destination.is_symlink())
        self.assert_no_editor()

    def test_missing_remote_tools_do_not_create_directories(self):
        (self.work / "bin" / "python3").symlink_to(sys.executable)
        self.env["PATH"] = str(self.work / "bin")
        for missing in ("git", "nvim"):
            if missing == "nvim":
                (self.work / "bin" / "nvim").unlink()
            with self.subTest(missing=missing):
                status, messages, stderr = self.helper_request(create=True)
                self.assertEqual(status, 1, stderr)
                expected = "Remote Neovim executable is missing" if missing == "nvim" else "git is not installed"
                self.assertIn(expected, messages[0]["message"])
                self.assertFalse(self.destination.parent.exists())
        self.assert_no_editor()

    def test_git_failure_removes_only_the_new_destination(self):
        self.install_git_stub()
        status, messages, stderr = self.helper_request(create=True)
        self.assertEqual(status, 1)
        self.assertEqual([m["type"] for m in messages], ["error"])
        self.assertIn("exit 9", messages[0]["message"])
        self.assertIn(str(self.destination), messages[0]["message"])
        self.assertIn("git stdout diagnostic", stderr)
        self.assertIn("git stderr diagnostic", stderr)
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.destination.parent.is_dir())
        self.assert_no_editor()

    def test_git_timeout_terminates_process_group_and_rolls_back(self):
        self.install_git_stub("stuck")
        status, messages, stderr = self.helper_request(create=True)
        self.assertEqual(status, 1, stderr)
        self.assertIn("timed out", messages[0]["message"])
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.destination.parent.is_dir())
        self.assert_no_editor()
        for event in self.events(self.env):
            self.assert_gone(event["pid"])

    def test_interrupted_initialization_terminates_process_group_and_rolls_back(self):
        self.install_git_stub("stuck")
        proc = self.launch_server(self.env)
        send(proc.stdin, "start", session=uuid.uuid4().hex, directory=str(self.destination), create=True)
        deadline = time.monotonic() + 4
        while not (self.work / "git-ready").exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.work / "git-ready").exists())
        proc.send_signal(signal.SIGTERM)
        _, stderr = proc.communicate(timeout=6)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, stderr.decode())
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.destination.parent.is_dir())
        self.assert_no_editor()
        for event in self.events(self.env):
            self.assert_gone(event["pid"])


class CreateSessionTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.env = self.fake_environment()
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull

    def run_create(self, destination):
        with patch.dict(os.environ, self.env, clear=True):
            run_session(Settings("example-host"), destination, create=True,
                        runtime_root=RUNTIME, remote_runtime_root=RUNTIME, timings=FAST)

    def assert_session_clean(self):
        for event in self.events(self.env):
            self.assert_gone(event["pid"])
            if "args" in event:
                args = event["args"]
                option = "--listen" if "--listen" in args else "--server"
                self.assertFalse(Path(args[args.index(option) + 1]).parent.exists())

    def test_absolute_path_with_spaces_and_quotes_opens_exact_directory(self):
        destination = self.work / "owner's project"
        self.run_create(str(destination))
        self.assertEqual((self.work / "selected-cwd").read_bytes(), os.fsencode(destination) + b"\n")
        self.assertTrue((destination / ".git").is_dir())
        self.assert_session_clean()

    def test_editor_failures_preserve_initialized_repository(self):
        for stage in ("startup", "ui"):
            destination = self.work / stage
            self.env["TEST_NVIM_MODE"] = "failure" if stage == "startup" else ""
            self.env["TEST_UI_MODE"] = "crash"
            with self.subTest(stage=stage), self.assertRaises(ButlerError):
                self.run_create(str(destination))
            self.assertTrue((destination / ".git" / "HEAD").is_file())
            self.assert_session_clean()

    def start_cli(self, mode):
        self.env["TEST_UI_MODE"] = mode
        code = """
from functools import partial
from unittest.mock import patch
from butler.cli import main
from butler.client import run_session
from tests.support import RUNTIME, FAST
session = partial(run_session, runtime_root=RUNTIME, remote_runtime_root=RUNTIME, timings=FAST)
with patch('butler.client.run_session', session):
    raise SystemExit(main(['create', '~/dev/owner/foo']))
"""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        saved = termios.tcgetattr(slave)
        proc = subprocess.Popen([sys.executable, "-c", code], env=self.env,
                                stdin=slave, stdout=slave, stderr=slave)
        self.addCleanup(self.close_process, proc)
        return proc, slave, saved

    def test_cli_expands_remote_home_creates_parents_and_restores_terminal(self):
        proc, slave, saved = self.start_cli("raw")
        self.assertEqual(proc.wait(timeout=8), 0)
        destination = self.work / "dev" / "owner" / "foo"
        self.assertTrue((destination / ".git").is_dir())
        result = subprocess.run(["git", "-C", str(destination), "rev-parse", "--show-toplevel"],
                                env=self.env, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), os.fsencode(destination))
        self.assertEqual((self.work / "selected-cwd").read_bytes(), os.fsencode(destination) + b"\n")
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assert_session_clean()

    def test_interrupting_editor_preserves_repository_and_cleans_session(self):
        proc, slave, saved = self.start_cli("wait")
        deadline = time.monotonic() + 5
        while not (self.work / "selected-cwd").exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.work / "selected-cwd").exists())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=8), 128 + signal.SIGTERM)
        self.assertTrue((self.work / "dev" / "owner" / "foo" / ".git").is_dir())
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assert_session_clean()
