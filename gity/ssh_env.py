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


def ssh_command_for(key_path: str | None, extra_opts: list[str] | None = None) -> str | None:
    """Return a GIT_SSH_COMMAND string, or None if no profile key is set.

    The command accepts new host keys automatically (batch-friendly) but
    still refuses changed keys, matching common CI practice.
    """
    if not key_path:
        return None
    key = Path(key_path).expanduser()
    if not key.exists():
        raise FileNotFoundError(f"SSH key not found: {key}")
    if os.name != "nt" and stat.S_IMODE(key.stat().st_mode) & 0o077:
        # ssh refuses private keys that are readable by group/other
        os.chmod(key, 0o600)
    opts = [
        "ssh",
        "-i", str(key),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
    ]
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
        return subprocess.run(cmd, env=env, cwd=cwd).returncode
    except FileNotFoundError:
        print("gity: git executable not found on PATH", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        # Ctrl-C reaches git too; report the conventional exit code quietly.
        return 130
