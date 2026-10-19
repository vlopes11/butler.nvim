"""OpenSSH subprocesses with an explicit lifetime and separate control streams."""

from pathlib import Path
import subprocess
import time

from .bootstrap import remote_command
from .config import validate_values
from .errors import ButlerError
from .protocol import Reader, send
from .runtime import RUNTIME_ROOT, StderrCapture, Timings, cleanup_signals, terminate


def command(host: str, action: str, forwarding: tuple[Path, Path] | None = None,
            *, runtime_root: Path = RUNTIME_ROOT, timings: Timings = Timings()) -> list[str]:
    validate_values({"host": host}, "SSH")
    if action not in {"list", "serve"}:
        raise ValueError("Unknown helper action")
    argv = ["ssh", "-T", "-S", "none"]
    options = {
        "ControlMaster": "no", "ControlPersist": "no",
        "ForkAfterAuthentication": "no", "StdinNull": "no",
        "SessionType": "default", "ExitOnForwardFailure": "yes",
        "ServerAliveInterval": "5", "ServerAliveCountMax": "3",
        "ConnectTimeout": "10", "StreamLocalBindMask": "0177",
        "StreamLocalBindUnlink": "no",
    }
    for key, value in options.items():
        argv.extend(["-o", f"{key}={value}"])
    if forwarding:
        argv.extend(["-L", f"{forwarding[0]}:{forwarding[1]}"])
    return [*argv, host, remote_command(action, runtime_root=runtime_root, timings=timings)]


class Connection:
    def __init__(self, host: str, action: str, forwarding=None, *,
                 runtime_root: Path = RUNTIME_ROOT, timings: Timings = Timings()):
        argv = command(host, action, forwarding, runtime_root=runtime_root, timings=timings)
        self.process = subprocess.Popen(argv,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.reader = Reader(self.process.stdout)
        self.stderr = StderrCapture(self.process.stderr)
        self.started = False
        self.stopped = False

    def receive(self, timeout: float = 0.1) -> list[dict]:
        messages = self.reader.receive(timeout)
        for message in messages:
            if message["type"] == "started":
                self.started = True
            elif message["type"] == "stopped":
                self.stopped = True
        return messages

    def send(self, kind: str, **fields):
        send(self.process.stdin, kind, **fields)

    def error(self, message: str) -> ButlerError:
        status = self.process.poll()
        if status is not None:
            self.stderr.thread.join(timeout=0.2)
        if status == 127 and not self.started:
            message += "; the remote host requires python3 (Python 3.11+) on its noninteractive SSH PATH"
        detail = self.stderr.text()
        return ButlerError(f"{message}\n{detail}" if detail else message)

    def close(self, *, stop: bool = False, grace: float = 3.0):
        with cleanup_signals():
            return self._close(stop=stop, grace=grace)

    def _close(self, *, stop: bool, grace: float):
        errors = []
        try:
            if stop and self.process.poll() is None:
                try:
                    self.send("stop")
                except (BrokenPipeError, OSError, ValueError):
                    pass
            self.process.stdin.close()
            deadline = time.monotonic() + grace + 2
            while not self.reader.eof and time.monotonic() < deadline:
                try:
                    for message in self.receive(0.05):
                        if message["type"] == "error":
                            errors.append(str(message.get("message", "Remote cleanup failed")))
                        elif message["type"] == "stopped":
                            reason = message.get("reason")
                            if reason == "neovim_exit" and message.get("returncode") != 0:
                                errors.append(f"Remote Neovim exited with status {message.get('returncode')}")
                            elif reason not in {"stop", "eof", "neovim_exit"}:
                                errors.append(f"Remote Neovim stopped unexpectedly: {reason}")
                except ButlerError as exc:
                    errors.append(str(exc))
                    break
            try:
                self.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                terminate(self.process, grace)
        finally:
            self.process.stdout.close()
            self.stderr.finish()
        if stop and self.started and not self.stopped:
            errors.append("Remote cleanup was not acknowledged; the remote heartbeat lease will expire")
        return errors
