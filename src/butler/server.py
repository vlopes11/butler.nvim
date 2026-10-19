"""Short-lived helpers invoked by SSH; no listening Butler service."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import BinaryIO, Callable

from .config import validate_values
from .errors import ButlerError, ConfigError, Interrupted
from .protocol import Reader, read_request, send
from .runtime import RUNTIME_ROOT, SessionPaths, Timings, cleanup_signals, signal_handlers, terminate


def main(action: str, *, runtime_root: Path = RUNTIME_ROOT,
         timings: Timings = Timings()) -> int:
    """Entry point for the helper supplied by the client, without a remote CLI."""
    try:
        with signal_handlers():
            # Keep queued heartbeats visible to Reader's unbuffered os.read.
            source = getattr(sys.stdin.buffer, "raw", sys.stdin.buffer)
            if action == "list":
                list_directories(source, sys.stdout.buffer)
            elif action == "serve":
                serve(source, sys.stdout.buffer, runtime_root=runtime_root, timings=timings)
            else:
                raise ButlerError(f"Unknown helper action: {action!r}")
    except Interrupted as exc:
        return 128 + exc.signum
    except (ButlerError, OSError) as exc:
        try:
            send(sys.stdout.buffer, "error", message=str(exc))
        except (OSError, ValueError):
            pass
        return 2 if isinstance(exc, ConfigError) else 1
    return 0


def directories(root: str, depth: int, warn: Callable[[str], None]) -> list[str]:
    validate_values({"root": root, "depth": depth}, "Directory request")
    expanded = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(expanded):
        raise ButlerError(f"Remote root is not a directory: {expanded}")
    current = [expanded]
    for level in range(depth):
        if not current:
            break
        following = []
        for parent in current:
            try:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                following.append(entry.path)
                        except OSError as exc:
                            warn(f"Skipping {entry.path!r}: {exc}")
            except OSError as exc:
                if level == 0:
                    raise ButlerError(f"Cannot read remote root {expanded!r}: {exc}") from None
                warn(f"Skipping {parent!r}: {exc}")
        current = following
    return sorted(current)


def list_directories(source: BinaryIO, output: BinaryIO) -> None:
    request = read_request(source, "list")
    if "root" not in request or "depth" not in request:
        raise ButlerError("Directory request requires root and depth")
    paths = directories(request["root"], request["depth"],
                        lambda message: send(output, "warning", message=message))
    for path in paths:
        send(output, "directory", path=path)
    send(output, "done")


def serve(source: BinaryIO, output: BinaryIO, *, runtime_root: Path = RUNTIME_ROOT,
          timings: Timings = Timings()) -> None:
    request = read_request(source, "start")
    paths = SessionPaths(request.get("session"), "server", runtime_root)
    directory = request.get("directory")
    if (not isinstance(directory, str) or "\0" in directory
            or not os.path.isabs(directory) or not os.path.isdir(directory)):
        raise ButlerError(f"Selected remote directory no longer exists or is invalid: {directory!r}")
    if shutil.which("nvim") is None:
        raise ButlerError("nvim is not installed on the server's noninteractive SSH PATH")
    child = None
    reason = "stop"
    returncode = None
    old_mask = os.umask(0o077)
    cleanup_errors = []
    try:
        paths.create()
        child = subprocess.Popen(
            ["nvim", "--headless", "--listen", str(paths.socket)],
            cwd=directory, stdin=subprocess.DEVNULL, stdout=sys.stderr,
            stderr=sys.stderr, start_new_session=True,
        )
        send(output, "started", session=request["session"])
        reader = Reader(source)
        last_heartbeat = time.monotonic()
        while True:
            returncode = child.poll()
            if returncode is not None:
                reason = "neovim_exit"
                break
            messages = reader.receive(timings.poll)
            stopping = False
            for message in messages:
                if message["type"] == "heartbeat":
                    last_heartbeat = time.monotonic()
                    send(output, "heartbeat")
                elif message["type"] == "stop":
                    stopping = True
                else:
                    raise ButlerError(f"Unexpected session command: {message['type']!r}")
            if stopping:
                break
            if reader.eof:
                reason = "eof"
                break
            if time.monotonic() - last_heartbeat >= timings.lease:
                reason = "heartbeat_timeout"
                break
    finally:
        active_error = sys.exc_info()[0] is not None
        with cleanup_signals():
            try:
                terminate(child, timings.shutdown, group=True)
            finally:
                cleanup_errors = paths.cleanup()
                os.umask(old_mask)
                if active_error:
                    for error in cleanup_errors:
                        print(f"butler: {error}", file=sys.stderr)
    if cleanup_errors:
        raise ButlerError("; ".join(cleanup_errors))
    try:
        send(output, "stopped", reason=reason, returncode=returncode)
    except BrokenPipeError:
        pass  # Cleanup is complete even if the client can no longer receive the acknowledgement.
