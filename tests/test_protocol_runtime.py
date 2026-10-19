import io
import os
import stat
import uuid

from tests.support import WorkspaceCase, ARTIFACTS
from butler.errors import ButlerError
from butler.protocol import Reader, decode, encode, read_request
from butler.runtime import SessionPaths


class ProtocolTests(WorkspaceCase):
    def test_roundtrip_unusual_path(self):
        path = "/a/' $()\n\t" + os.fsdecode(b"\xff")
        self.assertEqual(decode(encode("directory", path=path))["path"], path)

    def test_fragmented_and_coalesced_records(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb", buffering=0) as source, os.fdopen(write_fd, "wb", buffering=0) as sink:
            reader = Reader(source)
            record = encode("heartbeat")
            sink.write(record[:4])
            self.assertEqual(reader.receive(), [])
            sink.write(record[4:] + record)
            self.assertEqual([r["type"] for r in reader.receive()], ["heartbeat", "heartbeat"])
            sink.close()
            self.assertEqual(reader.receive(), [])
            self.assertTrue(reader.eof)

    def test_start_request_does_not_consume_following_heartbeat(self):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb", buffering=0) as source, os.fdopen(write_fd, "wb", buffering=0) as sink:
            sink.write(encode("start") + encode("heartbeat"))
            self.assertEqual(read_request(source, "start")["type"], "start")
            self.assertEqual(Reader(source).receive()[0]["type"], "heartbeat")

    def test_invalid_protocol(self):
        for record in (b"welcome!\n", b'{"version":99,"type":"heartbeat"}', b"[]", b'{"version":1}'):
            with self.subTest(record=record), self.assertRaises(ButlerError):
                decode(record)
        with self.assertRaises(ButlerError):
            read_request(io.BytesIO(encode("stop")), "start")


class ResourceTests(WorkspaceCase):
    def test_private_directory_and_repeatable_cleanup(self):
        paths = SessionPaths(uuid.uuid4().hex, "client", ARTIFACTS)
        paths.create()
        self.addCleanup(paths.cleanup)
        self.assertEqual(stat.S_IMODE(paths.directory.stat().st_mode), 0o700)
        self.assertEqual(paths.cleanup(), [])
        self.assertEqual(paths.cleanup(), [])

    def test_existing_directory_is_not_owned_or_deleted(self):
        paths = SessionPaths(uuid.uuid4().hex, "server", ARTIFACTS)
        paths.directory.mkdir()
        sentinel = paths.directory / "keep"
        sentinel.touch()
        self.addCleanup(paths.directory.rmdir)
        self.addCleanup(sentinel.unlink)
        with self.assertRaisesRegex(ButlerError, "Refusing existing"):
            paths.create()
        paths.cleanup()
        self.assertTrue(sentinel.exists())

    def test_session_id_and_path_length_validation(self):
        for session in (None, "../escape", "z" * 32):
            with self.assertRaises(ButlerError):
                SessionPaths(session, "server", ARTIFACTS)
        with self.assertRaisesRegex(ButlerError, "path limit"):
            SessionPaths("f" * 32, "server", self.work / ("a" * 108))
