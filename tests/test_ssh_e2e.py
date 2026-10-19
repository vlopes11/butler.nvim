"""Opt-in smoke test against a host explicitly supplied by the developer."""

import os
import pty
import select
import shlex
import shutil
import subprocess
import sys
import time
import unittest

from tests.support import ARTIFACTS, ROOT, UNIX_BIND_AVAILABLE, WorkspaceCase
from butler.client import discover
from butler.config import Settings

HOST = os.environ.get("BUTLER_TEST_SSH_HOST")


@unittest.skipUnless(HOST and UNIX_BIND_AVAILABLE and shutil.which("nvim") and shutil.which("ssh"),
                     "Set BUTLER_TEST_SSH_HOST and permit Unix socket binding to run the SSH smoke test")
class SSHSmokeTest(WorkspaceCase):
    def test_real_ui_and_forwarding_cleanup(self):
        check = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", HOST,
             "command -v python3 && command -v nvim"], capture_output=True, timeout=15,
        )
        self.assertEqual(check.returncode, 0, check.stderr.decode())
        root = os.environ.get("BUTLER_TEST_SSH_ROOT", "~")
        selected = discover(Settings(HOST, root, 0))
        self.assertEqual(len(selected), 1)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
        env["TEST_SELECTED_DIRECTORY"] = selected[0]
        env.setdefault("TERM", "xterm-256color")
        code = """
import os
from butler.client import run_session
from butler.config import Settings
from butler.runtime import signal_handlers, terminal_state
from butler.errors import Interrupted
from tests.support import ARTIFACTS
try:
    with signal_handlers(), terminal_state():
        run_session(Settings(os.environ['BUTLER_TEST_SSH_HOST']),
                    os.environ['TEST_SELECTED_DIRECTORY'], runtime_root=ARTIFACTS)
except Interrupted as exc:
    raise SystemExit(128 + exc.signum)
"""
        existing = set(ARTIFACTS.glob("butler-client-*"))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        child = subprocess.Popen([sys.executable, "-c", code], env=env,
                                 stdin=slave, stdout=slave, stderr=slave)
        self.addCleanup(self.close_process, child)
        transcript = bytearray()

        def drain():
            while select.select([master], [], [], 0)[0]:
                transcript.extend(os.read(master, 65536))

        deadline = time.monotonic() + 40
        local = None
        attached = False
        while time.monotonic() < deadline and child.poll() is None:
            drain()
            candidates = set(ARTIFACTS.glob("butler-client-*")) - existing
            if candidates:
                local = next(iter(candidates))
                try:
                    result = subprocess.run(
                        ["nvim", "--server", str(local / "nvim.sock"), "--remote-expr",
                         "len(nvim_list_uis())"], stdin=subprocess.DEVNULL,
                        capture_output=True, timeout=1,
                    )
                    attached = result.returncode == 0 and int(result.stdout.strip()) > 0
                except (subprocess.TimeoutExpired, ValueError):
                    pass
                if attached:
                    break
            time.sleep(0.05)
        self.assertTrue(attached, transcript.decode(errors="replace"))
        os.write(master, b"\x1b:qa!\r")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and child.poll() is None:
            drain()
            time.sleep(0.05)
        self.assertEqual(child.poll(), 0, transcript.decode(errors="replace"))
        self.assertFalse(local.exists())
        session = local.name.removeprefix("butler-client-")
        remote = f"/tmp/butler-server-{session}"
        check = subprocess.run(
            ["ssh", "-T", "-o", "BatchMode=yes", HOST,
             shlex.join(["test", "!", "-e", remote])], capture_output=True, timeout=15,
        )
        self.assertEqual(check.returncode, 0, check.stderr.decode())
