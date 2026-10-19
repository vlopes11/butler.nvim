"""Local profiles. Remote paths remain unexpanded until they reach the server."""

import os
from dataclasses import asdict, dataclass
from pathlib import Path
import tomllib

from .errors import ConfigError

DEFAULT_HOST = "remote-host"


@dataclass(frozen=True)
class Settings:
    host: str = DEFAULT_HOST
    root: str = "~/dev"
    depth: int = 2


def validate_values(values: dict, label: str) -> None:
    unknown = values.keys() - {"host", "root", "depth"}
    if unknown:
        raise ConfigError(f"{label}: unknown settings: {', '.join(sorted(unknown))}")
    if "host" in values:
        host = values["host"]
        if (not isinstance(host, str) or not host or host.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in host)):
            raise ConfigError(f"{label}: host must be an SSH destination or alias")
    if "root" in values:
        root = values["root"]
        if (not isinstance(root, str) or "\0" in root
                or not (root.startswith("/") or root == "~" or root.startswith("~/"))):
            raise ConfigError(f"{label}: root must be an absolute path or start with ~/")
    if "depth" in values:
        depth = values["depth"]
        if type(depth) is not int or depth < 0:
            raise ConfigError(f"{label}: depth must be a nonnegative integer")


def load_settings(profile: str | None = None, config: Path | None = None,
                  *, host: str | None = None, root: str | None = None,
                  depth: int | None = None) -> Settings:
    explicit = config is not None
    if config is None:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        config = base / "butler" / "config.toml"
    try:
        with config.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"Configuration file does not exist: {config}") from None
        document = {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read configuration {config}: {exc}") from None
    if document.keys() - {"profiles"}:
        raise ConfigError("Configuration only accepts a [profiles] table")
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("profiles must be a TOML table")
    for name, values in profiles.items():
        if not name or not isinstance(values, dict):
            raise ConfigError("Each profile must be a named TOML table")
        validate_values(values, f"Profile {name!r}")
    selected = profile if profile is not None else "default"
    if selected not in profiles and selected != "default":
        raise ConfigError(f"Unknown profile: {selected!r}")
    values = asdict(Settings())
    values.update(profiles.get("default", {}))
    values.update(profiles.get(selected, {}))
    values.update({key: value for key, value in
                   {"host": host, "root": root, "depth": depth}.items()
                   if value is not None})
    validate_values(values, "Resolved configuration")
    return Settings(**values)
