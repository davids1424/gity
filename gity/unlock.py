"""Short-lived unlock cache for password-protected profile keys.

`gity pull && gity push` runs gity twice, and a single git command can open
several SSH connections. Asking for the password every time would be
miserable, so the first SSH connection that needs a protected key unlocks
it into a private ssh-agent and later connections reuse it.

The cache is deliberately narrow:

* One agent per terminal session (os.getsid), with its socket in
  ~/.gity/run/s<sid>/ (mode 0700). A gity started from another session —
  e.g. an AI agent's shell, which runs in its own session with no
  terminal — never looks there and gets its own prompt instead.
* Nothing is cached unless a terminal is attached: no terminal, no prompt,
  no unlock.
* The agent is started as `ssh-agent -a SOCK sleep N`, so it exits on its
  own after N seconds (default 60, GITY_UNLOCK_SECONDS overrides) and the
  key is added with the same lifetime. Nothing stays unlocked afterwards.
* The password is typed straight into ssh-add on /dev/tty; it never passes
  through gity, argv or the environment.

This is protection against casual access, not against hostile code running
as your own OS user during that window — see the README security notes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .profiles import gity_home

DEFAULT_SECONDS = 60


def unlock_seconds() -> int:
    try:
        val = int(os.environ.get("GITY_UNLOCK_SECONDS", DEFAULT_SECONDS))
    except ValueError:
        val = DEFAULT_SECONDS
    return max(1, min(val, 3600))


def have_terminal() -> bool:
    """Can a password prompt be shown on /dev/tty?"""
    if os.name == "nt":
        return sys.stdin.isatty()
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


# Unix socket paths are limited to ~104 bytes (macOS) / 108 (Linux).
_SOCK_MAX = 100


def _private_dir(path: Path) -> Path:
    """Create `path` as a 0700 directory owned by us, or refuse."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if path.is_symlink() or st.st_uid != os.getuid():
        raise PermissionError(f"{path} is not a private directory owned by you")
    os.chmod(path, 0o700)
    return path


def _run_roots() -> list[Path]:
    """Where per-session agent sockets live: ~/.gity/run, or a short
    private directory in /tmp when GITY_HOME is too deep for a socket."""
    return [gity_home() / "run", Path("/tmp") / f"gity-{os.getuid()}"]


def _run_root() -> Path:
    home_run = _run_roots()[0]
    probe = home_run / f"s{os.getsid(0)}" / "agent.sock"
    return home_run if len(str(probe)) <= _SOCK_MAX else _run_roots()[1]


def _session_dir() -> Path:
    run = _private_dir(_run_root())
    d = run / f"s{os.getsid(0)}"
    return _private_dir(d)


def _ssh_add(sock: Path, *args: str, interactive: bool = False) -> int:
    env = dict(os.environ, SSH_AUTH_SOCK=str(sock), SSH_ASKPASS_REQUIRE="never")
    env.pop("SSH_ASKPASS", None)
    kw = {} if interactive else {"stdin": subprocess.DEVNULL,
                                 "stdout": subprocess.DEVNULL,
                                 "stderr": subprocess.DEVNULL}
    try:
        return subprocess.run(["ssh-add", *args], env=env, **kw).returncode
    except FileNotFoundError:
        return 127


def _agent_alive(sock: Path) -> bool:
    # ssh-add -l: 0 = has keys, 1 = no keys, 2 = cannot reach the agent
    return sock.exists() and _ssh_add(sock, "-l") in (0, 1)


def _start_agent(sock: Path, seconds: int) -> bool:
    if sock.exists() or sock.is_symlink():
        sock.unlink()
    try:
        # the agent exits by itself when `sleep` does
        subprocess.Popen(["ssh-agent", "-a", str(sock), "sleep", str(seconds)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        return False
    for _ in range(100):
        if _agent_alive(sock):
            return True
        time.sleep(0.02)
    return False


def unlocked_agent(key_path: str) -> str | None:
    """Return an agent socket holding `key_path` unlocked, prompting if needed.

    Returns None when there is no terminal, ssh-agent is unavailable, or the
    password was wrong; ssh then falls back to asking on its own (and fails
    cleanly without a terminal).
    """
    if os.name == "nt" or not have_terminal():
        return None
    pub = Path(key_path + ".pub")
    try:
        sock = _session_dir() / "agent.sock"
    except OSError as e:
        print(f"gity: cannot set up the unlock cache ({e}); ssh will ask for the password "
              "on every connection", file=sys.stderr)
        return None
    alive = _agent_alive(sock)
    if alive and pub.exists() and _ssh_add(sock, "-T", str(pub)) == 0:
        return str(sock)  # already unlocked in this terminal session
    if alive:
        # An older agent's clock is already running; empty it and start a
        # fresh one so this unlock gets its full window.
        _ssh_add(sock, "-D")
    seconds = unlock_seconds()
    if not _start_agent(sock, seconds):
        print("gity: could not start a private ssh-agent; ssh will ask for the password "
              "on every connection", file=sys.stderr)
        return None
    print(f"gity: this profile is password-protected (stays unlocked {seconds}s "
          "in this terminal only)", file=sys.stderr)
    if _ssh_add(sock, "-q", "-t", str(seconds), key_path, interactive=True) != 0:
        return None
    return str(sock)


def lock_all() -> None:
    """Forget every cached unlock (used by tests and `gity profile --lock`)."""
    for run in _run_roots():
        if not run.is_dir() or run.is_symlink() or run.lstat().st_uid != os.getuid():
            continue
        for d in run.glob("s*"):
            sock = d / "agent.sock"
            if _agent_alive(sock):
                _ssh_add(sock, "-D")
            shutil.rmtree(d, ignore_errors=True)
