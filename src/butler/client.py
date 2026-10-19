"""Directory selection and supervision of one local UI / remote editor pair."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from .config import Settings, validate_remote_path
from .errors import ButlerError
from .runtime import RUNTIME_ROOT, SessionPaths, Timings, cleanup_signals, terminal_state, terminate
from .ssh import Connection


def discover(settings: Settings) -> list[str]:
    connection = Connection(settings.host, "list")
    paths = []
    done = False
    try:
        connection.send("list", root=settings.root, depth=settings.depth)
        connection.process.stdin.close()
        while not connection.reader.eof:
            for message in connection.receive():
                kind = message["type"]
                if done:
                    raise ButlerError("Unexpected output after directory listing completed")
                if kind == "directory":
                    path = message.get("path")
                    if not isinstance(path, str) or "\0" in path or not os.path.isabs(path):
                        raise ButlerError("Server returned an invalid directory path")
                    paths.append(path)
                elif kind == "warning":
                    print(f"butler: {message.get('message', 'remote warning')}", file=sys.stderr)
                elif kind == "done":
                    done = True
                elif kind == "error":
                    raise ButlerError(str(message.get("message", "Remote listing failed")))
                else:
                    raise ButlerError(f"Unexpected listing response: {kind!r}")
        try:
            status = connection.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            raise ButlerError("SSH did not exit after directory listing") from None
        if status != 0 or not done:
            raise ButlerError(f"Remote directory listing failed (SSH exit {status})")
    except (ButlerError, BrokenPipeError) as exc:
        raise connection.error(str(exc)) from None
    finally:
        connection.close()
    return paths


def pick(paths: list[str], host: str) -> str | None:
    if not paths:
        raise ButlerError("No remote directories match the configured root and exact depth")
    result = subprocess.run(
        ["fzf", "--read0", "--print0", "--no-multi", "--prompt", f"{host}> "],
        input=b"".join(os.fsencode(path) + b"\0" for path in paths), stdout=subprocess.PIPE,
    )
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise ButlerError(f"fzf failed (exit {result.returncode})")
    if not result.stdout.endswith(b"\0") or result.stdout.count(b"\0") != 1:
        raise ButlerError("fzf did not return one NUL-delimited directory")
    chosen = os.fsdecode(result.stdout[:-1])
    if chosen not in paths:
        raise ButlerError("fzf returned a directory outside the supplied selection")
    return chosen


def probe(socket: Path, timeout: float) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["nvim", "--server", str(socket), "--remote-expr", "1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "Neovim RPC did not respond"
    return (result.returncode == 0 and result.stdout.strip() == b"1",
            result.stderr.decode("utf-8", errors="replace").strip())


def run_session(settings: Settings, directory: str, *, runtime_root: Path = RUNTIME_ROOT,
                remote_runtime_root: Path = RUNTIME_ROOT,
                timings: Timings = Timings(), create: bool = False) -> None:
    session = uuid.uuid4().hex
    local = SessionPaths(session, "client", runtime_root)
    remote = SessionPaths(session, "server", remote_runtime_root)
    connection = None
    ui = None
    cleanup_errors = []
    try:
        local.create()
        connection = Connection(settings.host, "serve", (local.socket, remote.socket),
                                runtime_root=remote_runtime_root, timings=timings)
        request = {"session": session, "directory": directory}
        if create:
            request["create"] = True
        connection.send("start", **request)
        handshake_deadline = time.monotonic() + timings.handshake
        startup_deadline = None
        next_heartbeat = time.monotonic()
        last_response = time.monotonic()
        next_probe = 0.0
        probe_error = ""
        ssh_exit_seen = None
        while True:
            if ui is not None and ui.poll() is not None:
                if ui.returncode:
                    raise ButlerError(f"Local Neovim UI exited with status {ui.returncode}")
                return
            for message in connection.receive(timings.poll):
                kind = message["type"]
                last_response = time.monotonic()
                if kind == "started":
                    if startup_deadline is not None or message.get("session") != session:
                        raise ButlerError("Unexpected session startup response")
                    startup_deadline = time.monotonic() + timings.startup
                elif kind == "heartbeat":
                    pass
                elif kind == "error":
                    raise ButlerError(str(message.get("message", "Remote session failed")))
                elif kind == "stopped":
                    if (ui is not None and message.get("reason") == "neovim_exit"
                            and message.get("returncode") == 0):
                        try:
                            ui.wait(timeout=timings.shutdown)
                        except subprocess.TimeoutExpired:
                            terminate(ui, timings.shutdown)
                        return
                    raise ButlerError(f"Remote Neovim stopped: {message.get('reason')} (exit {message.get('returncode')})")
                else:
                    raise ButlerError(f"Unexpected session response: {kind!r}")
            if connection.reader.eof:
                # An orderly UI exit may race the final SSH EOF.
                if ui is not None and ui.poll() == 0:
                    return
                raise ButlerError("SSH session ended unexpectedly")
            now = time.monotonic()
            if connection.process.poll() is not None:
                # Allow one more read for the final status record. Do not rely on
                # EOF alone: a child of a failed SSH process may still hold stdout.
                if ssh_exit_seen is None:
                    ssh_exit_seen = now
                elif now - ssh_exit_seen >= timings.poll:
                    raise ButlerError(f"SSH exited unexpectedly (status {connection.process.returncode})")
            if startup_deadline is None and now >= handshake_deadline:
                raise ButlerError("Timed out waiting for the SSH helper to start")
            if startup_deadline is not None and now - last_response >= timings.lease:
                raise ButlerError("Remote helper stopped responding")
            if now >= next_heartbeat:
                connection.send("heartbeat")
                next_heartbeat = now + timings.heartbeat
            if ui is None and startup_deadline is not None:
                if now >= startup_deadline:
                    raise ButlerError(f"Remote Neovim did not become ready within {timings.startup:g}s: {probe_error}")
                if now >= next_probe and local.socket.exists():
                    ready, probe_error = probe(local.socket, min(timings.probe, startup_deadline - now))
                    next_probe = time.monotonic() + 0.2
                    if ready:
                        ui = subprocess.Popen(["nvim", "--server", str(local.socket), "--remote-ui"])
    except (ButlerError, BrokenPipeError) as exc:
        if connection is not None:
            raise connection.error(str(exc)) from None
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        with cleanup_signals():
            try:
                terminate(ui, timings.shutdown)
            finally:
                try:
                    if connection is not None:
                        cleanup_errors.extend(connection.close(stop=True, grace=timings.shutdown))
                finally:
                    cleanup_errors.extend(local.cleanup())
                    if cleanup_errors:
                        if active_error:
                            for error in cleanup_errors:
                                print(f"butler: {error}", file=sys.stderr)
                        else:
                            raise ButlerError("; ".join(cleanup_errors))


def connect(settings: Settings) -> None:
    with terminal_state():
        for executable in ("ssh", "fzf", "nvim"):
            if shutil.which(executable) is None:
                raise ButlerError(f"Required client executable is missing: {executable}")
        paths = discover(settings)
        directory = pick(paths, settings.host)
        if directory is not None:
            run_session(settings, directory)


def create(settings: Settings, directory: str) -> None:
    validate_remote_path(directory, "Destination")
    with terminal_state():
        for executable in ("ssh", "nvim"):
            if shutil.which(executable) is None:
                raise ButlerError(f"Required client executable is missing: {executable}")
        run_session(settings, directory, create=True)
