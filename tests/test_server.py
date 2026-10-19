import os
import signal
import uuid

from tests.support import WorkspaceCase, RUNTIME
from butler.protocol import Reader, send
from butler.runtime import SessionPaths


class ServerTests(WorkspaceCase):
    def start(self, env=None):
        session = uuid.uuid4().hex
        paths = SessionPaths(session, "server", RUNTIME)
        proc = self.launch_server(env or self.fake_environment())
        reader = Reader(proc.stdout)
        send(proc.stdin, "start", session=session, directory=str(self.work))
        self.await_message(reader, "started")
        return proc, reader, paths

    def assert_clean(self, proc, paths, pid):
        self.assertEqual(proc.wait(timeout=5), 0, proc.stderr.read().decode())
        self.assertFalse(paths.directory.exists())
        self.assert_gone(pid)

    def test_nvim_cwd_and_explicit_stop(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        self.assertEqual(self.rpc(paths.socket, "getcwd()").stdout.strip(), os.fsencode(self.work))
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        send(proc.stdin, "stop")
        result = self.await_message(reader, "stopped")
        self.assertEqual(result["reason"], "stop")
        self.assert_clean(proc, paths, pid)

    def test_eof_cleanup(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        proc.stdin.close()
        self.assertEqual(self.await_message(reader, "stopped")["reason"], "eof")
        self.assert_clean(proc, paths, pid)

    def test_heartbeat_expiry(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        self.assertEqual(self.await_message(reader, "stopped")["reason"], "heartbeat_timeout")
        self.assert_clean(proc, paths, pid)

    def test_nvim_quit_cleans_server_socket(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        os.kill(pid, signal.SIGTERM)
        message = self.await_message(reader, "stopped")
        self.assertEqual(message["reason"], "neovim_exit")
        self.assert_clean(proc, paths, pid)

    def test_concurrent_sessions_are_isolated(self):
        first, first_reader, first_paths = self.start()
        second, second_reader, second_paths = self.start()
        self.ready(first, first_paths.socket)
        self.ready(second, second_paths.socket)
        first_pid = int(self.rpc(first_paths.socket, "getpid()").stdout)
        second_pid = int(self.rpc(second_paths.socket, "getpid()").stdout)
        send(first.stdin, "stop")
        self.await_message(first_reader, "stopped")
        self.assert_clean(first, first_paths, first_pid)
        self.assertEqual(self.rpc(second_paths.socket, "1").stdout.strip(), b"1")
        send(second.stdin, "stop")
        self.await_message(second_reader, "stopped")
        self.assert_clean(second, second_paths, second_pid)

    def test_invalid_control_message_still_cleans_up(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        send(proc.stdin, "bogus")
        self.assertEqual(proc.wait(timeout=4), 1)
        self.assertFalse(paths.directory.exists())
        self.assert_gone(pid)

    def test_signal_cleans_up(self):
        proc, reader, paths = self.start()
        self.ready(proc, paths.socket)
        pid = int(self.rpc(paths.socket, "getpid()").stdout)
        proc.send_signal(signal.SIGHUP)
        self.assertEqual(proc.wait(timeout=4), 128 + signal.SIGHUP)
        self.assertFalse(paths.directory.exists())
        self.assert_gone(pid)

    def test_unresponsive_child_is_killed(self):
        env = self.fake_environment()
        env["TEST_NVIM_MODE"] = "stuck"
        proc, reader, paths = self.start(env)
        self.assertEqual(self.await_message(reader, "stopped")["reason"], "heartbeat_timeout")
        self.assertEqual(proc.wait(timeout=4), 0)
        for event in self.events(env):
            self.assert_gone(event["pid"])
        self.assertFalse(paths.directory.exists())
