"""Small, versioned JSON records on SSH's non-PTY standard streams."""

import json
import os
import select
from typing import BinaryIO

from .errors import ButlerError

VERSION = 1
MAX_RECORD = 1024 * 1024


def encode(kind: str, **fields) -> bytes:
    return (json.dumps({"version": VERSION, "type": kind, **fields}, ensure_ascii=True)
            + "\n").encode("ascii")


def decode(line: bytes) -> dict:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError):
        raise ButlerError("Invalid helper output; SSH shell startup files must not print to stdout") from None
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ButlerError("Unexpected protocol from the remote Python helper")
    if not isinstance(value.get("type"), str):
        raise ButlerError("Helper message is missing its type")
    return value


def send(stream: BinaryIO, kind: str, **fields) -> None:
    data = encode(kind, **fields)
    if len(data) > MAX_RECORD:
        raise ButlerError("Helper message is too large")
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:
            raise BrokenPipeError("Helper input closed")
        view = view[written:]
    stream.flush()


def read_request(stream: BinaryIO, expected: str) -> dict:
    line = stream.readline(MAX_RECORD + 1)
    if len(line) > MAX_RECORD or not line.endswith(b"\n"):
        raise ButlerError("Incomplete or oversized helper request")
    request = decode(line)
    if request["type"] != expected:
        raise ButlerError(f"Expected a {expected!r} helper request")
    return request


class Reader:
    """Read complete records without blocking the session supervision loop."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.buffer = bytearray()
        self.eof = False

    def receive(self, timeout: float = 0.1) -> list[dict]:
        if self.eof:
            return []
        if not select.select([self.stream], [], [], max(0, timeout))[0]:
            return []
        chunk = os.read(self.stream.fileno(), 65536)
        if not chunk:
            self.eof = True
            if self.buffer:
                raise ButlerError("Helper connection ended inside a message")
            return []
        self.buffer.extend(chunk)
        records = []
        while b"\n" in self.buffer:
            line, _, remaining = self.buffer.partition(b"\n")
            self.buffer = bytearray(remaining)
            if len(line) > MAX_RECORD:
                raise ButlerError("Helper message is too large")
            records.append(decode(line))
        if len(self.buffer) > MAX_RECORD:
            raise ButlerError("Helper message is too large")
        return records
