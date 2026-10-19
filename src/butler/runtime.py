"""Resources owned by one invocation, never a persistent service."""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import termios
import threading
from typing import BinaryIO

from .errors import ButlerError, Interrupted

RUNTIME_ROOT = Path("/tmp")


@dataclass(frozen=True)
class Timings:
    heartbeat: float = 5.0
    lease: float = 20.0
    startup: float = 30.0
    handshake: float = 60.0
    shutdown: float = 3.0
    probe: float = 1.0
    poll: float = 0.1


class SessionPaths:
    def __init__(self, session: str, role: str, root: Path = RUNTIME_ROOT):
        if not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{32}", session):
            raise ButlerError("Invalid session UUID")
        if role not in {"client", "server"}:
            raise ValueError("Invalid session role")
        self.directory = root / f"butler-{role}-{session}"
        self.socket = self.directory / "nvim.sock"
        if len(os.fsencode(self.socket)) >= 108:
            raise ButlerError("Session socket path exceeds Linux's Unix socket path limit")
        self.owned = False

    def create(self) -> None:
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            raise ButlerError(f"Refusing existing session directory: {self.directory}") from None
        self.owned = True

    def cleanup(self) -> list[str]:
        if not self.owned:
            return []
        errors = []
        try:
            self.socket.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"Cannot remove {self.socket}: {exc}")
        try:
            self.directory.rmdir()
            self.owned = False
        except FileNotFoundError:
            self.owned = False
        except OSError as exc:
            errors.append(f"Cannot remove {self.directory}: {exc}")
        return errors


def terminate(process: subprocess.Popen | None, grace: float = 3.0,
              *, group: bool = False) -> None:
    if process is None:
        return

    def deliver(signum):
        try:
            if group:
                os.killpg(process.pid, signum)
            elif process.poll() is None:
                process.send_signal(signum)
        except ProcessLookupError:
            pass

    deliver(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        deliver(signal.SIGKILL)
        process.wait()
    finally:
        # Also stop any descendants left in our own process group after its leader exits.
        if group:
            deliver(signal.SIGKILL)


class StderrCapture:
    """Drain stderr continuously, retaining a bounded diagnostic tail in memory."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while chunk := self.stream.read(4096):
                with self.lock:
                    self.buffer.extend(chunk)
                    del self.buffer[:-65536]
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        with self.lock:
            return self.buffer.decode("utf-8", errors="replace").strip()

    def finish(self):
        self.thread.join(timeout=1)
        self.stream.close()


@contextmanager
def cleanup_signals():
    """Let an already-started cleanup finish even if another signal arrives."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in watched}
    try:
        for sig in watched:
            signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def signal_handlers():
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.getsignal(sig) for sig in watched}

    def interrupt(signum, _frame):
        # A second signal must not interrupt the cleanup of the first one.
        for sig in watched:
            signal.signal(sig, signal.SIG_IGN)
        raise Interrupted(signum)

    try:
        for sig in watched:
            signal.signal(sig, interrupt)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def terminal_state():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ButlerError("Run Butler in an interactive terminal")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass  # The terminal may already have been closed.
