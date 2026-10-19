import os
import subprocess
import sys
import uuid
from unittest.mock import patch

from butler.bootstrap import remote_command
from butler.protocol import decode, encode
from tests.support import ARTIFACTS, FAST, WorkspaceCase


class BootstrapTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.bin = self.work / "bin"
        self.bin.mkdir()
        self.python = self.bin / "python3"
        self.python.symlink_to(sys.executable)
        self.env = self.environment()
        # There is no Butler executable on this PATH. Poison Python's normal
        # import locations as well: the remote helper must be self-contained.
        self.env["PATH"] = str(self.bin)
        self.env["PYTHONPATH"] = str(self.work)
        self.env["PYTHONHOME"] = str(self.work / "missing-python-home")
        for name in ("butler.py", "json.py", "sitecustomize.py"):
            (self.work / name).write_text("raise RuntimeError('imported remote environment')\n")

    def run_helper(self, action, request):
        command = remote_command(action, runtime_root=ARTIFACTS, timings=FAST)
        return subprocess.run(["/bin/sh", "-c", command], input=request, cwd=self.work,
                              env=self.env, capture_output=True, timeout=5)

    def test_discovery_without_remote_installation(self):
        root = self.work / "space ' quote\n$()"
        project = root / os.fsdecode(b"project-\xff")
        project.mkdir(parents=True)
        result = self.run_helper("list", encode("list", root=str(root), depth=1))
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        messages = [decode(line) for line in result.stdout.splitlines()]
        self.assertEqual([m["path"] for m in messages if m["type"] == "directory"], [str(project)])
        self.assertEqual(messages[-1]["type"], "done")
        self.assertEqual(result.stderr, b"")

    def test_missing_neovim_is_reported_without_session_artifacts(self):
        session = uuid.uuid4().hex
        result = self.run_helper("serve", encode("start", session=session, directory=str(self.work)))
        self.assertNotEqual(result.returncode, 0)
        error = decode(result.stdout)
        self.assertEqual(error["type"], "error")
        self.assertIn("nvim", error["message"])
        self.assertFalse((ARTIFACTS / f"butler-server-{session}").exists())

    def test_unsupported_python_reports_version_requirement(self):
        self.python.unlink()
        # Exercise the bootstrap's version gate without requiring another
        # interpreter installation. Do not load the bundled source on 3.10.
        self.python.write_text(
            f"#!{sys.executable} -I\n"
            "import sys\n"
            "sys.version_info = (3, 10, 0)\n"
            "exec(sys.argv[sys.argv.index('-c') + 1])\n"
        )
        self.python.chmod(0o755)
        result = self.run_helper("list", encode("list", root=str(self.work), depth=0))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Python 3.11", result.stderr)

    def test_bootstrap_failure_is_reported_on_stderr(self):
        with patch("butler.bootstrap._sources", return_value=(("broken", "raise RuntimeError('broken payload')"),)):
            result = self.run_helper("list", encode("list", root=str(self.work), depth=0))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Cannot start remote Python helper", result.stderr)
        self.assertIn(b"broken payload", result.stderr)
