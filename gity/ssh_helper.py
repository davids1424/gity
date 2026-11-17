"""GIT_SSH_COMMAND for password-protected profiles.

git runs `python -m gity.ssh_helper KEY <ssh args...>` instead of ssh. The
helper unlocks KEY into this terminal session's short-lived agent (asking
for the password only if it isn't unlocked yet), then execs the real ssh
pointed at that agent. Nothing happens — and nothing is asked — unless git
actually opens an SSH connection, so `gity status` never prompts.
"""

from __future__ import annotations

import os
import sys

from .ssh_env import protected_ssh_argv
from .unlock import unlocked_agent


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m gity.ssh_helper KEY [ssh args...]", file=sys.stderr)
        return 255
    key, rest = argv[0], argv[1:]
    sock = unlocked_agent(key)
    cmd = protected_ssh_argv(key, agent_sock=sock) + rest
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"gity: cannot run ssh: {e}", file=sys.stderr)
        return 255
    return 255  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
