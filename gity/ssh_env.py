"""SSH integration: git commands run with GIT_SSH_COMMAND pointing at the
right profile key. New host keys are accepted automatically so first-time
clones don't hang on an interactive prompt; changed host keys are still
refused."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

from .profiles import key_is_encrypted
from .unlock import have_terminal

# Options for password-protected profile keys. The unlocked key must only
# ever live in gity's own short-lived, per-terminal agent (see unlock.py):
#   IdentityAgent        gity's agent, or none — never your normal ssh-agent
#   AddKeysToAgent=no    override `AddKeysToAgent yes` in ~/.ssh/config
#   UseKeychain=no       macOS: never store the password in the Keychain
#                        (IgnoreUnknown keeps non-Apple OpenSSH happy)
#   no password/keyboard-interactive auth: the key password is the only prompt
_PROTECTED_OPTS = [
    "-o", "IgnoreUnknown=UseKeychain",
    "-o", "UseKeychain=no",
    "-o", "AddKeysToAgent=no",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
]


def _base_opts(key: str) -> list[str]:
    return [
        "ssh",
        "-i", key,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
    ]


def protected_ssh_argv(key_path: str, agent_sock: str | None = None) -> list[str]:
    """ssh argv for a password-protected key (used by gity.ssh_helper)."""
    agent = agent_sock.replace("%", "%%") if agent_sock else "none"
    return _base_opts(key_path) + _PROTECTED_OPTS + ["-o", f"IdentityAgent={agent}"]


def ssh_command_for(key_path: str | None, extra_opts: list[str] | None = None) -> str | None:
    """Return a GIT_SSH_COMMAND string, or None if no profile key is set.

    The command accepts new host keys automatically (batch-friendly) but
    still refuses changed keys, matching common CI practice. For a
    password-protected key it goes through gity.ssh_helper, which unlocks
    the key once per terminal session instead of once per connection.
    """
    if not key_path:
        return None
    key = Path(key_path).expanduser()
    if not key.exists():
        raise FileNotFoundError(f"SSH key not found: {key}")
    if os.name != "nt" and stat.S_IMODE(key.stat().st_mode) & 0o077:
        # ssh refuses private keys that are readable by group/other
        os.chmod(key, 0o600)
    if key_is_encrypted(key):
        # run the helper from THIS gity installation, whatever cwd/PYTHONPATH say
        pkg_parent = str(Path(__file__).resolve().parents[1])
        boot = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from gity.ssh_helper import main; sys.exit(main(sys.argv[1:]))")
        opts = [sys.executable, "-c", boot, pkg_parent, str(key)]
    else:
        opts = _base_opts(str(key)) + ["-o", "BatchMode=yes"]
    if extra_opts:
        opts += extra_opts
    return " ".join(shlex.quote(str(o)) for o in opts)


def run_git_with_profile(git_args: list[str], key_path: str | None,
                         cwd: str | None = None, capture: bool = False) -> int:
    """Run `git ...` with GIT_SSH_COMMAND bound to a profile key."""
    env = os.environ.copy()
    try:
        cmd_str = ssh_command_for(key_path)
    except FileNotFoundError as e:
        print(f"gity: {e}", file=sys.stderr)
        print("gity: the profile's key file is missing — re-import it (gity --import FILE) "
              "or remove the profile (gity profile --remove NAME)", file=sys.stderr)
        return 2
    if cmd_str:
        env["GIT_SSH_COMMAND"] = cmd_str
        env["GIT_SSH_VARIANT"] = "ssh"
    cmd = ["git"] + list(git_args)
    # anything gity printed so far must appear before git's own output,
    # even when stdout is a pipe and therefore block-buffered
    sys.stdout.flush()
    try:
        if capture:
            proc = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
            if proc.stdout:
                print(proc.stdout, end="")
            if proc.stderr:
                print(proc.stderr, end="", file=sys.stderr)
            return proc.returncode
        # subprocess (not execvpe) so gity can act AFTER git finishes —
        # e.g. bind a cloned repo to its profile. stdio is inherited, so
        # colors, paging, prompts and signals behave exactly like plain git.
        rc = subprocess.run(cmd, env=env, cwd=cwd).returncode
        if rc != 0 and key_path and key_is_encrypted(Path(key_path).expanduser()) \
                and not have_terminal():
            print("gity: this profile is password-protected and no terminal is available "
                  "to ask for its password — run the command yourself in a terminal",
                  file=sys.stderr)
        return rc
    except FileNotFoundError:
        print("gity: git executable not found on PATH", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        # Ctrl-C reaches git too; report the conventional exit code quietly.
        return 130
