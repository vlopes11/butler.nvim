import os
from pathlib import Path
from unittest.mock import patch

from tests.support import WorkspaceCase
from butler.config import Settings, load_settings
from butler.errors import ConfigError, ButlerError
from butler.server import directories


class ConfigurationTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.config = self.work / "config.toml"
        self.config.write_text('''[profiles.default]
host = "example-host"
root = "~/dev"
depth = 2
[profiles.work]
root = "~/work"
[profiles.other]
host = "other"
depth = 0
''')

    def test_default_and_inheritance(self):
        self.assertEqual(load_settings(config=self.config), Settings("example-host"))
        self.assertEqual(load_settings("work", self.config), Settings("example-host", "~/work", 2))
        self.assertEqual(load_settings("other", self.config), Settings("other", "~/dev", 0))

    def test_overrides_and_remote_home_not_expanded(self):
        self.assertEqual(load_settings("work", self.config, host="user@host", root="~/else", depth=3),
                         Settings("user@host", "~/else", 3))

    def test_unknown_profile_and_missing_config(self):
        with self.assertRaisesRegex(ConfigError, "Unknown profile"):
            load_settings("typo", self.config)
        with self.assertRaisesRegex(ConfigError, "does not exist"):
            load_settings(config=self.work / "missing")

    def test_implicit_config_and_builtin_defaults(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.work)}):
            self.assertEqual(load_settings(host="example-host"), Settings("example-host"))
            self.assertEqual(load_settings(), Settings("remote-host"))
            (self.work / "butler").mkdir()
            (self.work / "butler" / "config.toml").write_bytes(self.config.read_bytes())
            self.assertEqual(load_settings("work"), Settings("example-host", "~/work", 2))

    def test_invalid_configuration(self):
        documents = [
            '[profiles.default]\ndepth = -1', '[profiles.default]\ndepth = true',
            '[profiles.default]\nroot = "relative"', '[profiles.default]\nhost = "-evil"',
            '[profiles.default]\nroot = 5', '[profiles.default]\nunknown = 1',
            'profiles = 1', 'default = "example-host"', 'invalid toml',
        ]
        for document in documents:
            with self.subTest(document=document):
                self.config.write_text(document)
                with self.assertRaises(ConfigError):
                    load_settings(config=self.config)


class DirectoryTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.dev = self.work / "dev"
        for relative in ("owner/project/deeper", "owner/space ' quote\nand newline",
                         ".hidden/project", "other/project"):
            (self.dev / relative).mkdir(parents=True)
        (self.dev / "owner" / "file").touch()
        (self.dev / "linked").symlink_to(self.dev / "owner", target_is_directory=True)

    def test_exact_depth_and_hidden_directories(self):
        self.assertEqual(directories(str(self.dev), 0, self.fail), [str(self.dev)])
        expected = [str(self.dev / name) for name in (".hidden", "other", "owner")]
        self.assertEqual(directories(str(self.dev), 1, self.fail), expected)
        actual = directories(str(self.dev), 2, self.fail)
        self.assertEqual(len(actual), 4)
        self.assertIn(str(self.dev / "owner" / "space ' quote\nand newline"), actual)
        self.assertFalse(any("linked" in name or "deeper" in name for name in actual))

    def test_remote_home_and_empty_results(self):
        with patch.dict(os.environ, {"HOME": str(self.work)}):
            self.assertEqual(directories("~/dev", 0, self.fail), [str(self.dev)])
        self.assertEqual(directories(str(self.dev), 5, self.fail), [])

    def test_invalid_root_and_permission_failure(self):
        with self.assertRaisesRegex(ButlerError, "not a directory"):
            directories(str(self.work / "missing"), 2, self.fail)
        original = os.scandir
        warnings = []

        def deny_one(directory):
            if Path(directory) == self.dev / "owner":
                raise PermissionError("denied")
            return original(directory)

        with patch("butler.server.os.scandir", side_effect=deny_one):
            results = directories(str(self.dev), 2, warnings.append)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(warnings), 1)
