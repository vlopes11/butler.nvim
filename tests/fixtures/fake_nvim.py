import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

args = sys.argv[1:]
role = "ui" if "--remote-ui" in args else "server" if "--headless" in args else "probe"
with open(os.environ["TEST_EVENTS"], "a") as stream:
    stream.write(json.dumps({"role": role, "pid": os.getpid(), "args": args}) + "\n")
real = os.environ["TEST_REAL_NVIM"]
simulated = os.environ.get("TEST_SIMULATE_RPC")
if role != "ui":
    if role == "server" and os.environ.get("TEST_NVIM_DELAY"):
        time.sleep(float(os.environ["TEST_NVIM_DELAY"]))
    if role == "server" and os.environ.get("TEST_NVIM_MODE") == "stuck":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    if role == "server" and os.environ.get("TEST_NVIM_MODE") == "failure":
        print("intentional Neovim startup failure", file=sys.stderr)
        sys.exit(9)
    if simulated:
        if role == "server":
            signal.signal(signal.SIGUSR1, lambda *_: sys.exit(0))
            path = Path(args[args.index("--listen") + 1])
            path.write_text(json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}))
            while True:
                time.sleep(1)
        try:
            state = json.loads(Path(args[args.index("--server") + 1]).read_text())
            os.kill(state["pid"], 0)
        except (OSError, ValueError):
            sys.exit(1)
        if "--remote-send" in args:
            os.kill(state["pid"], signal.SIGUSR1)
        else:
            expression = args[args.index("--remote-expr") + 1]
            print({"1": 1, "getpid()": state["pid"], "getcwd()": state["cwd"]}[expression])
        sys.exit(0)
    os.execv(real, [real, *args])

mode = os.environ.get("TEST_UI_MODE", "detach")
if mode == "real":
    os.execv(real, [real, *args])
server = args[args.index("--server") + 1]
rpc = [sys.executable, __file__] if simulated else [real]
cwd = subprocess.run([*rpc, "--server", server, "--remote-expr", "getcwd()"],
                     stdout=subprocess.PIPE, timeout=2, check=True).stdout
Path(os.environ["TEST_PROJECT"], "selected-cwd").write_bytes(cwd)
if mode == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "wait":
    while True:
        time.sleep(1)
if mode == "quit":
    subprocess.run([*rpc, "--server", server, "--remote-send", ":qa!\r"], timeout=2)
    time.sleep(0.1)
if mode == "server_crash":
    pid = subprocess.run([*rpc, "--server", server, "--remote-expr", "getpid()"],
                         stdout=subprocess.PIPE, check=True, timeout=2).stdout
    os.kill(int(pid), signal.SIGKILL)
if mode == "raw":
    import tty
    tty.setraw(sys.stdin.fileno())
sys.exit(0)
