import argparse
from pathlib import Path
import sys

from . import __version__
from .client import connect, create
from .config import DEFAULT_HOST, load_settings
from .errors import ButlerError, ConfigError, Interrupted
from .runtime import signal_handlers


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="butler", description="Temporary remote Neovim sessions over SSH")
    cli.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = cli.add_subparsers(dest="command", required=True)
    client = commands.add_parser("connect", help="Select a remote directory and open Neovim")
    client.add_argument("--root", help="Override the remote search root; quote '~' to expand it remotely")
    client.add_argument("--depth", type=int, help="Exact directory depth; zero selects the root")
    creator = commands.add_parser("create", help="Create a remote Git repository and open Neovim")
    creator.add_argument("path", metavar="PATH", help="New remote directory; quote '~' to expand it remotely")
    for command in (client, creator):
        command.add_argument("profile", nargs="?", help="Profile name (default: default)")
        command.add_argument("--host", help=f"Override the SSH destination or alias (built-in default: {DEFAULT_HOST})")
        command.add_argument("--config", type=Path, help="Read profiles from this TOML file")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        with signal_handlers():
            settings = load_settings(args.profile, args.config, host=args.host,
                                     root=getattr(args, "root", None), depth=getattr(args, "depth", None))
            if args.command == "create":
                create(settings, args.path)
            else:
                connect(settings)
    except Interrupted as exc:
        return 128 + exc.signum
    except (ButlerError, OSError) as exc:
        print(f"butler: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 1
    return 0
