import argparse
from pathlib import Path
import sys

from . import __version__
from .client import connect
from .config import DEFAULT_HOST, load_settings
from .errors import ButlerError, ConfigError, Interrupted
from .runtime import signal_handlers


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="butler", description="Temporary remote Neovim sessions over SSH")
    cli.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = cli.add_subparsers(dest="command", required=True, metavar="{connect}")
    client = commands.add_parser("connect", help="Select a remote directory and open Neovim")
    client.add_argument("profile", nargs="?", help="Profile name (default: default)")
    client.add_argument("--host", help=f"Override the SSH destination or alias (built-in default: {DEFAULT_HOST})")
    client.add_argument("--root", help="Override the remote search root; quote '~' to expand it remotely")
    client.add_argument("--depth", type=int, help="Exact directory depth; zero selects the root")
    client.add_argument("--config", type=Path, help="Read profiles from this TOML file")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with signal_handlers():
            settings = load_settings(args.profile, args.config, host=args.host,
                                     root=args.root, depth=args.depth)
            connect(settings)
    except Interrupted as exc:
        return 128 + exc.signum
    except (ButlerError, OSError) as exc:
        print(f"butler: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 1
    return 0
