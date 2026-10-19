"""Local SSH stand-in: real Unix forwarding and an actual remote helper subprocess."""
import os
import json
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time

args = sys.argv[1:]
with open(os.environ["TEST_EVENTS"], "a") as stream:
    stream.write(json.dumps({"role": "ssh", "pid": os.getpid()}) + "\n")
mode = os.environ.get("TEST_SSH_MODE")
if mode in {"authentication_failure", "forward_failure"}:
    print(mode, file=sys.stderr)
    sys.exit(255)

listener = None
finished = threading.Event()


def bridge(local):
    remote = socket.socket(socket.AF_UNIX)
    try:
        remote.connect(remote_path)
        while not finished.is_set():
            readable, _, _ = select.select([local, remote], [], [], 0.1)
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (remote if source is local else local).sendall(data)
    except OSError:
        pass
    finally:
        local.close()
        remote.close()


def accept():
    while not finished.is_set():
        try:
            client, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        threading.Thread(target=bridge, args=(client,), daemon=True).start()


if "-L" in args:
    local_path, remote_path = args[args.index("-L") + 1].split(":", 1)
    if os.environ.get("TEST_SIMULATE_RPC"):
        Path(local_path).symlink_to(remote_path)
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(local_path)
        listener.listen()
        listener.settimeout(0.1)
        threading.Thread(target=accept, daemon=True).start()

environment = os.environ.copy()
environment.pop("PYTHONPATH", None)
if mode == "missing_python":
    environment["PATH"] = str(Path(environment["HOME"]) / "missing-bin")
helper = subprocess.Popen(["/bin/sh", "-c", args[-1]], env=environment,
                          cwd=environment["HOME"])
with open(os.environ["TEST_EVENTS"], "a") as stream:
    stream.write(json.dumps({"role": "helper", "pid": helper.pid}) + "\n")


def interrupted(signum, _frame):
    helper.terminate()


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGHUP, interrupted)
try:
    if mode == "disconnect":
        marker = Path(os.environ["TEST_PROJECT"]) / "selected-cwd"
        while not marker.exists() and helper.poll() is None:
            time.sleep(0.02)
        if helper.poll() is None:
            helper.send_signal(signal.SIGHUP)
    sys.exit(helper.wait())
finally:
    finished.set()
    if listener:
        listener.close()
