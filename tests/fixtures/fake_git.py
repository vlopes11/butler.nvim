"""Git initialization failures, including a process tree that outlives a timeout."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

if sys.argv[1:] != ["init"]:
    sys.exit(2)

Path(".git").mkdir()
Path(".git/partial").write_text("incomplete initialization")
print("git stdout diagnostic", flush=True)
print("git stderr diagnostic", file=sys.stderr, flush=True)

if os.environ.get("TEST_GIT_MODE") != "stuck":
    sys.exit(9)

child = subprocess.Popen([sys.executable, "-c",
                          "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_DFL); time.sleep(60)"])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["TEST_EVENTS"], "a") as stream:
    for role, pid in (("git", os.getpid()), ("git-child", child.pid)):
        stream.write(json.dumps({"role": role, "pid": pid}) + "\n")
Path(os.environ["TEST_PROJECT"], "git-ready").touch()
while True:
    child.poll()  # Reap the child after group termination, even while Git ignores SIGTERM.
    time.sleep(0.01)
