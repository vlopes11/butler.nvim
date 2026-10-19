"""Bundle the remote helper as source for an isolated, in-memory Python process."""

import base64
from dataclasses import asdict
from functools import cache
from importlib.resources import files
import json
from pathlib import Path
import shlex
import zlib

from .runtime import RUNTIME_ROOT, Timings


# Register each module before executing it: relative imports and dataclasses
# need its entry in sys.modules. This order follows the helper's dependencies.
_MODULES = ("errors", "config", "protocol", "runtime", "server")
_BOOTSTRAP = """import sys
if sys.version_info < (3, 11):
    sys.exit("butler: Remote Python 3.11 or newer is required")
try:
    import base64, json, types, zlib
    from pathlib import Path
    bundle = json.loads(zlib.decompress(base64.b64decode(__PAYLOAD__)))
    package = types.ModuleType("_butler_remote")
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name, source in bundle["modules"]:
        module = types.ModuleType(package.__name__ + "." + name)
        module.__package__ = package.__name__
        module.__file__ = "<butler-remote/" + name + ".py>"
        sys.modules[module.__name__] = module
        setattr(package, name, module)
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    runtime_root = Path(bundle["runtime_root"])
    timings = package.runtime.Timings(**bundle["timings"])
except Exception as exc:
    sys.exit("butler: Cannot start remote Python helper: " + str(exc))
sys.exit(package.server.main(sys.argv[1], runtime_root=runtime_root, timings=timings))
"""


@cache
def _sources() -> tuple[tuple[str, str], ...]:
    package = files(__package__)
    return tuple((name, package.joinpath(name + ".py").read_text(encoding="utf-8"))
                 for name in _MODULES)


def remote_command(action: str, *, runtime_root: Path = RUNTIME_ROOT,
                   timings: Timings = Timings()) -> str:
    if action not in {"list", "serve"}:
        raise ValueError("Unknown helper action")
    bundle = {"modules": _sources(), "runtime_root": str(runtime_root),
              "timings": asdict(timings)}
    payload = base64.b64encode(zlib.compress(json.dumps(bundle).encode("ascii"))).decode("ascii")
    bootstrap = _BOOTSTRAP.replace("__PAYLOAD__", repr(payload))
    # Only bundled code and internal settings go in the command. User requests
    # stay on stdin, which -c leaves free for the JSON control protocol.
    return "exec " + shlex.join(["python3", "-I", "-S", "-B", "-u", "-c", bootstrap, action])
