import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from butler.bootstrap import remote_command
from butler.protocol import send
from butler.runtime import Timings, terminate

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".test-artifacts"

FAST = Timings(heartbeat=0.1, lease=2, startup=2, handshake=2, shutdown=0.2, probe=0.2, poll=0.02)


def socket_binding_available():
    ARTIFACTS.mkdir(exist_ok=True)
    probe = ARTIFACTS / f"bind-{os.getpid()}"
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            listener.bind(str(probe))
        except PermissionError:
            return False
        else:
            probe.unlink()
            return True


UNIX_BIND_AVAILABLE = socket_binding_available()


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        ARTIFACTS.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ARTIFACTS, prefix="t-")
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def environment(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
        env["HOME"] = str(self.work)
        env["XDG_CONFIG_HOME"] = str(self.work / "config")
        env["XDG_DATA_HOME"] = str(self.work / "data")
        env["XDG_STATE_HOME"] = str(self.work / "state")
        env["XDG_CACHE_HOME"] = str(self.work / "cache")
        env["XDG_RUNTIME_DIR"] = str(self.work / "run")
        Path(env["XDG_RUNTIME_DIR"]).mkdir(exist_ok=True, mode=0o700)
        env["NVIM_APPNAME"] = "butler-tests"
        env.pop("NVIM", None)
        env.pop("FZF_DEFAULT_OPTS", None)
        env.pop("FZF_DEFAULT_OPTS_FILE", None)
        env["TEST_PROJECT"] = str(self.work)
        env["TEST_EVENTS"] = str(self.work / "events.jsonl")
        env["TEST_REAL_NVIM"] = shutil.which("nvim") or ""
        env.pop("TEST_SIMULATE_RPC", None)
        return env

    def fake_environment(self):
        env = self.environment()
        binaries = self.work / "bin"
        binaries.mkdir(exist_ok=True)
        for executable in ("ssh", "nvim", "fzf"):
            target = ROOT / "tests" / "fixtures" / f"fake_{executable}.py"
            # Use a launcher rather than requiring executable bits on test fixture sources.
            script = binaries / executable
            script.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(target)!r}, run_name='__main__')\n")
            script.chmod(0o755)
        env["PATH"] = str(binaries) + os.pathsep + env["PATH"]
        env["TEST_SIMULATE_RPC"] = "1"
        return env

    def launch_server(self, env=None):
        self.active_env = dict(env or self.environment())
        self.active_env.pop("PYTHONPATH", None)
        command = remote_command("serve", runtime_root=ARTIFACTS, timings=FAST)
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.work,
                                env=self.active_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.addCleanup(self.close_process, proc)
        return proc

    @staticmethod
    def close_process(proc):
        terminate(proc, 0.3)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def await_message(self, reader, kind, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in reader.receive(0.05):
                if message["type"] == "error":
                    self.fail(message["message"])
                if message["type"] == kind:
                    return message
            if reader.eof:
                self.fail(f"EOF waiting for {kind}")
        self.fail(f"Timed out waiting for {kind}")

    def rpc(self, socket, expression):
        env = self.active_env
        return subprocess.run([shutil.which("nvim", path=env["PATH"]), "--server", str(socket),
                               "--remote-expr", expression], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=2, env=env)

    def ready(self, proc, socket, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            send(proc.stdin, "heartbeat")
            if socket.exists():
                result = self.rpc(socket, "1")
                if result.returncode == 0 and result.stdout.strip() == b"1":
                    return
            if proc.poll() is not None:
                self.fail(proc.stderr.read().decode())
            time.sleep(0.03)
        self.fail("Neovim did not become ready")

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def events(self, env):
        path = Path(env["TEST_EVENTS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
