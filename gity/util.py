"""Helpers shared by gity modules."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SSH_SCHEMES = {"ssh", "git+ssh", "ssh+git"}


def looks_like_scp(url: str) -> bool:
    """True for scp-like syntax (`[user@]host:path`).

    Scheme URLs (`https://...`), local paths (`/srv/repo`, `./repo`) and
    Windows drive paths (`C:\\repo`) are not scp-like. Git's own rule is the
    same: a colon that appears before any slash marks an scp-style remote.
    """
    if _SCHEME_RE.match(url) or _WIN_DRIVE_RE.match(url):
        return False
    colon = url.find(":")
    return colon > 0 and "/" not in url[:colon]


def is_ssh_url(url: str) -> bool:
    """True when git would reach this remote over SSH."""
    if looks_like_scp(url):
        return True
    m = _SCHEME_RE.match(url)
    return bool(m) and url[:m.end() - 3].lower() in _SSH_SCHEMES


def ssh_target(url: str) -> tuple[str | None, str, int | None] | None:
    """Split an SSH remote into (user, host, port), or None if it isn't SSH."""
    if looks_like_scp(url):
        head = url.split(":", 1)[0]
        user, _, host = head.rpartition("@")
        return (user or None, host, None) if host else None
    if not is_ssh_url(url):
        return None
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return parsed.username or None, host, port


def url_to_repo_name(url: str) -> str:
    """Directory name `git clone <url>` creates when none is given."""
    path = url.strip().replace("\\", "/").rstrip("/")
    if path.endswith("/.git"):
        path = path[:-5].rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    if looks_like_scp(url) and ":" in tail:  # host:repo.git (no slash in path)
        tail = tail.rsplit(":", 1)[-1]
    for suffix in (".git", ".bundle"):
        if tail.endswith(suffix):
            tail = tail[:-len(suffix)]
            break
    return tail or "repo"
