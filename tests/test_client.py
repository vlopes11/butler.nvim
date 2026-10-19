import io
import os
from pathlib import Path
import pty
import signal
import subprocess
import sys
import termios
import time
from unittest.mock import patch

from tests.support import WorkspaceCase, RUNTIME, FAST
from butler.client import discover, pick, run_session
from butler.config import Settings
from butler.errors import ButlerError


class PickerTests(WorkspaceCase):
    def test_preserves_unusual_paths_and_cancellation(self):
        env = self.fake_environment()
        paths = ["/space ' \n directory", "/other"]
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(pick(paths, "example-host"), paths[0])
            os.environ["TEST_FZF_CANCEL"] = "1"
            self.assertIsNone(pick(paths, "example-host"))

    def test_empty_list(self):
        with self.assertRaisesRegex(ButlerError, "No remote directories"):
            pick([], "example-host")

    def test_ssh_directory_discovery(self):
        env = self.fake_environment()
        (self.work / "dev" / "owner" / "project").mkdir(parents=True)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(discover(Settings("example-host", "~/dev", 2)),
                             [str(self.work / "dev" / "owner" / "project")])

    def test_ssh_authentication_failure(self):
        env = self.fake_environment()
        env["TEST_SSH_MODE"] = "authentication_failure"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ButlerError, "authentication_failure"):
                discover(Settings("example-host"))

    def test_missing_remote_python(self):
        env = self.fake_environment()
        env["TEST_SSH_MODE"] = "missing_python"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ButlerError, "Python 3.11"):
                discover(Settings("example-host"))


class ClientSessionTests(WorkspaceCase):
    def run_with(self, env):
        with patch.dict(os.environ, env, clear=True):
            run_session(Settings("example-host"), str(self.work), runtime_root=RUNTIME,
                        remote_runtime_root=RUNTIME, timings=FAST)

    def assert_resources_removed(self, env):
        for event in self.events(env):
            self.assert_gone(event["pid"])
            if "args" not in event:
                continue
            args = event["args"]
            option = "--listen" if "--listen" in args else "--server"
            socket = Path(args[args.index(option) + 1])
            self.assertFalse(socket.exists())
            self.assertFalse(socket.parent.exists())

    def test_ui_detach_cleans_both_sides(self):
        env = self.fake_environment()
        self.run_with(env)
        self.assertEqual((self.work / "selected-cwd").read_bytes().strip(), os.fsencode(self.work))
        self.assert_resources_removed(env)

    def test_delayed_readiness(self):
        env = self.fake_environment()
        env["TEST_NVIM_DELAY"] = "0.4"
        self.run_with(env)
        self.assert_resources_removed(env)

    def test_ssh_disconnect_stops_local_ui(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "wait"
        env["TEST_SSH_MODE"] = "disconnect"
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(ButlerError):
                self.run_with(env)
        self.assert_resources_removed(env)

    def test_remote_crash_is_not_success_when_ui_exits_cleanly(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "server_crash"
        with self.assertRaises(ButlerError):
            self.run_with(env)
        self.assert_resources_removed(env)

    def test_ui_crash_cleans_both_sides(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "crash"
        with self.assertRaises(ButlerError):
            self.run_with(env)
        self.assert_resources_removed(env)

    def test_normal_remote_quit_is_not_a_connection_error(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "quit"
        self.run_with(env)
        self.assert_resources_removed(env)

    def test_remote_startup_failure(self):
        env = self.fake_environment()
        env["TEST_NVIM_MODE"] = "failure"
        with self.assertRaisesRegex(ButlerError, "intentional Neovim startup failure"):
            self.run_with(env)
        self.assert_resources_removed(env)

    def test_readiness_timeout_cleans_unresponsive_editor(self):
        env = self.fake_environment()
        env["TEST_NVIM_MODE"] = "stuck"
        with self.assertRaises(ButlerError):
            self.run_with(env)
        self.assert_resources_removed(env)

    def test_forwarding_failure_removes_local_directory(self):
        env = self.fake_environment()
        env["TEST_SSH_MODE"] = "forward_failure"
        before = set(RUNTIME.glob("butler-client-*"))
        with self.assertRaises(ButlerError):
            self.run_with(env)
        self.assertEqual(set(RUNTIME.glob("butler-client-*")), before)

    def start_terminal_session(self, env):
        code = """
import os
from pathlib import Path
from butler.client import run_session
from butler.config import Settings
from butler.errors import Interrupted
from butler.runtime import terminal_state, signal_handlers
from tests.support import FAST, RUNTIME
try:
    with signal_handlers(), terminal_state():
        run_session(Settings('example-host'), os.environ['TEST_PROJECT'], runtime_root=RUNTIME,
                    remote_runtime_root=RUNTIME, timings=FAST)
except Interrupted as exc:
    raise SystemExit(128 + exc.signum)
"""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        saved = termios.tcgetattr(slave)
        proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=slave,
                                stdout=slave, stderr=slave)
        self.addCleanup(self.close_process, proc)
        return proc, master, slave, saved

    def test_terminal_restored_after_ui_changes_modes(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "raw"
        proc, master, slave, saved = self.start_terminal_session(env)
        self.assertEqual(proc.wait(timeout=8), 0)
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assert_resources_removed(env)

    def test_sigterm_cleans_active_session(self):
        env = self.fake_environment()
        env["TEST_UI_MODE"] = "wait"
        proc, master, slave, saved = self.start_terminal_session(env)
        deadline = time.monotonic() + 5
        while not (self.work / "selected-cwd").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.work / "selected-cwd").exists())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=8), 128 + signal.SIGTERM)
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assert_resources_removed(env)
