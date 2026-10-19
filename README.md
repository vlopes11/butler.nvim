# Butler

Butler opens a local Neovim UI for a temporary headless Neovim session on a Linux
server. Pick a remote project with local fzf or create a new Git repository, edit
through an SSH Unix socket forward, and close the UI to stop the session and
remove its sockets.

There is no Butler daemon. Each invocation owns its SSH processes, a remote
supervisor, and a headless editor. Independent invocations can run concurrently.

## Install

Install Butler **on the client only**, using Python 3.11 or newer:

```sh
uv tool install .
# Or, inside a Python virtual environment:
python3 -m pip install .
```

The client also needs OpenSSH and Neovim with `--remote-ui`. The `connect` command
additionally needs local fzf with `--read0` / `--print0`; `create` skips the picker.
The server needs OpenSSH server, **Python 3.11 or newer** with its standard library,
and Neovim with `--headless`, `--listen`, and remote RPC support. The `create`
command also requires Git on the server's noninteractive SSH PATH.
Compatible Neovim versions are required; using the same version on both machines
avoids UI protocol differences.

Butler supplies its remote helper through the SSH command and runs it in memory.
There is no remote package installation, uploaded script file, or helper cache.
An existing remote Butler installation is ignored and may be removed.

Ensure `python3` and `nvim` are on the server's **noninteractive SSH PATH**:

```sh
ssh remote-host 'python3 --version; command -v nvim'
```

If Python or Neovim is installed in a user directory (such as `~/.local/bin`),
add that directory to the PATH initialized by the remote shell for noninteractive
commands. The remote shell's startup files must not print banners to stdout,
which carries Butler's control protocol.

The SSH server must allow local stream-local forwarding, for example
`AllowStreamLocalForwarding local` or `yes`, with `DisableForwarding no`. Account
or authorized-key restrictions must also permit forwarding and remote commands.
Butler does not change SSH configuration.

## Profiles and usage

Create `$XDG_CONFIG_HOME/butler/config.toml`, or
`~/.config/butler/config.toml` when `XDG_CONFIG_HOME` is unset:

```toml
[profiles.default]
host = "remote-host"
root = "~/dev"
depth = 2

[profiles.work]
root = "~/work"

[profiles.other]
host = "another-ssh-alias"
depth = 3
```

Run Butler inside your existing terminal:

```sh
butler connect
butler connect work
butler connect work --depth 3
butler connect --host remote-host --root '~/dev' --depth 2
butler connect --config ./profiles.toml
```

No profile selects `default`. Named profiles inherit missing settings from
`default`; unknown names are errors. CLI flags override profile settings. The
built-in defaults are host `remote-host`, root `~/dev`, and depth `2`. The default
configuration file is optional. Define `remote-host` as an alias in your SSH
configuration, or override it with a profile's `host` setting or `--host`.

Depth is **exact**, relative to the root. At depth 2, `~/dev/vlopes11/butler`
appears but `~/dev/vlopes11` does not. Depth 0 offers the root itself. Hidden
directories are included, directory symlinks are skipped, and unreadable
descendants are skipped with a warning. Invalid or unreadable roots are errors.
Quote `~` in CLI arguments so it expands on the server instead of the client.

fzf runs locally and lists absolute remote directory paths. Selecting one starts
Neovim in that directory. Esc cancels without starting an editor. No matches are
reported as an error. The headless editor loads the server user's normal Neovim
configuration, so plugins, language servers, and filesystem access run remotely.

To create a new remote project and open it directly:

```sh
butler create '~/dev/vlopes11/foo'
butler create '~/dev/vlopes11/foo' work
butler create '/srv/projects/foo' --host my-server --config ./profiles.toml
```

`create PATH [PROFILE]` accepts the complete destination as an absolute path or
a quoted `~/` path, expanded on the server. It uses the same profile selection,
`--host`, and `--config` options as `connect`. Profile search root and depth do
not affect creation; `create` does not accept `--root` or `--depth`.

Missing parents are created. An existing destination, including an empty
directory, file, or symlink, is rejected without changing it. Butler runs
`git init` in the new directory using the remote user's Git defaults, including
the initial branch, then opens Neovim there. It creates no commit or Git remote.

If Git initialization fails, times out after 30 seconds, or is interrupted,
Butler removes the newly created destination and partial Git files, leaving any
new parent directories. Cleanup failures are reported with the affected path.
Once Git succeeds, the repository is kept even if the editor fails or the
session is interrupted. Closing Neovim removes only the temporary session
resources, just as with `connect`.

SSH aliases, users, identities, ports, jump hosts, authentication, and host-key
checking come from your normal SSH configuration. SSH may prompt in your terminal
before the editor starts. Butler disables connection sharing and persistence for
its own connections and does not close unrelated SSH masters.

## Session lifecycle

1. A short SSH command runs the bundled directory helper using
   `python3 -I -S -B -u -c ... list` and returns directories. Python runs with
   package paths isolated and site initialization disabled, using the helper
   source supplied by the client and the standard library.
2. Local fzf selects one directory; no editor is running yet.
3. Butler creates a private `/tmp/butler-client-<uuid>/` directory and starts a
   managed SSH subprocess with `-L LOCAL_SOCKET:REMOTE_SOCKET` and the bundled
   Python supervisor's `serve` action. That same SSH connection carries both
   the forwarding and the supervisor's control messages.
4. The remote helper creates `/tmp/butler-server-<uuid>/` and starts
   `nvim --headless --listen .../nvim.sock` in the selected directory.
5. Butler confirms RPC works through the forwarded socket before starting
   `nvim --server LOCAL_SOCKET --remote-ui` in your terminal.
6. On UI exit, Butler requests shutdown, waits for remote cleanup, terminates its
   remaining SSH process, and removes the local socket and directory.

`create` skips discovery and selection (steps 1–2). The same remote supervisor
creates the destination and initializes Git before starting Neovim in step 4.

Both directories have mode `0700`; socket access is restricted to their owner.
Existing session directories are never reused or removed. No TCP listeners are
created by Butler.

The supervisor handles normal quit, UI detach or crash, Ctrl-C, SIGTERM, SIGHUP,
startup errors, and SSH failure. It sends heartbeats every 5 seconds; a remote
helper with no heartbeat for 20 seconds terminates its editor. SSH also uses
5-second server-alive checks with three missed responses. Neovim gets SIGTERM,
then SIGKILL if it has not exited within 3 seconds. Readiness has a 30-second
deadline after the remote process starts; the initial SSH/helper handshake has a
60-second deadline. Local terminal modes are restored when Butler exits.

Butler never issues a buffer-save command. Save your changes before closing the
UI. Existing Neovim recovery files are left intact; recovery availability depends
on your Neovim settings. There is no reconnect or persistent-session feature.

SIGKILL of the local supervisor or a client machine crash prevents local cleanup
from running; local socket directories may remain. The running remote helper
still expires its heartbeat lease. If the server supervisor itself is forcibly
killed or its machine crashes, its cleanup cannot run either. Butler does not
run a background sweeper. Cleanup errors and unacknowledged remote shutdowns are
reported rather than presented as successful cleanup.

Exit status is 0 for success or picker cancellation, 1 for runtime failures, 2 for
configuration/argument errors, and `128 + signal` for an interrupted supervisor.

## Development and tests

There are no third-party runtime Python dependencies. With Python 3.11+, Git,
`just`, and `uv` installed, run the CI checks with:

```sh
just check
```

This runs Ruff against `src` and `tests`, then the unittest suite, stopping on the
first failure. Linting reports problems without rewriting files. The Ruff version
is pinned in `justfile`; `uv` downloads it on first use and caches it for later
runs. `just lint` and `just test` run each check separately. GitHub Actions runs
the same `just check` command on pushes and pull requests using Python 3.11.

To run only the test suite without `just` or `uv`:

```sh
PYTHONPATH=src python3 -m unittest discover -v
```

The suite uses real subprocesses and pseudo-terminals to exercise lifecycle,
signals, readiness, profile handling, unusual filenames, and cleanup. Simulated
SSH, Neovim, and fzf fixtures keep the default suite independent of installed
tools and allow it to run in sandboxes that forbid socket binding. The SSH fixture
executes the real bundled Python helper without importing the remote helper from
the checkout. Tests also verify startup with no remote Butler executable and
with Python environment variables pointing to unusable package locations.
Creation tests use real Git for successful initialization and a Git fixture for
failure, timeout, and interruption cases. Test projects stay under
`.test-artifacts/` in the checkout. Session sockets use a separate temporary
directory shared by the test subprocesses and removed when the suite exits, so
long checkout paths do not exceed the Unix socket path limit. In a restricted
environment, set `TMPDIR` to a short, writable temporary directory.
The optional SSH test below exercises real Neovim and Unix socket forwarding.

To run the optional test through actual SSH, use a dedicated test host with
Python 3.11+ and Neovim, preferably with Butler absent. Make sure noninteractive
SSH authentication works, then set:

```sh
BUTLER_TEST_SSH_HOST=my-test-host \
BUTLER_TEST_SSH_ROOT='~/dev' \
PYTHONPATH=src python3 -m unittest tests.test_ssh_e2e -v
```

This test starts a real remote editor, attaches a real local UI in a
pseudo-terminal, quits it, and checks that both session directories are gone.
Use a test account whose Neovim startup is suitable for unattended execution.

## License

Licensed under either the [Apache License, Version 2.0](LICENSE-APACHE) or the
[MIT license](LICENSE-MIT), at your option.
