"""Profile storage for gity.

Profiles live in ~/.gity (overridable with GITY_HOME). Layout:

    ~/.gity/
        config.json             # profiles metadata + repo assignments + default
        keys/<profile>_key      # private key
        keys/<profile>_key.pub  # public key

config.json shape:
{
  "default_profile": "work",
  "auto_detect": true,
  "profiles": {
    "work": {
      "user_name": "Ada Lovelace",
      "email": "ada@example.com",
      "comment": "work laptop",
      "key_type": "ed25519",
      "key_path": "~/.gity/keys/work_key",
      "created_at": "2026-09-25T09:00:00+00:00"
    }
  },
  "repos": {
    "/abs/path/to/repo": "personal"
  }
}
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Profile names become file names (keys/<name>_key), so keep them boring:
# no path separators, no leading dash/dot, nothing a shell would mangle.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_FILE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]{0,63})_key(\.pub)?$")

KEY_TYPES = ("ed25519", "rsa", "ecdsa")
EXPORT_VERSION = 1


def valid_name(name: object) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


def gity_home() -> Path:
    env = os.environ.get("GITY_HOME")
    return Path(env).expanduser() if env else Path.home() / ".gity"


def config_path() -> Path:
    return gity_home() / "config.json"


def keys_dir() -> Path:
    return gity_home() / "keys"


def _ensure_dirs() -> Path:
    """Create the gity home and a private keys directory; return the latter."""
    gity_home().mkdir(parents=True, exist_ok=True)
    kdir = keys_dir()
    kdir.mkdir(mode=0o700, exist_ok=True)
    return kdir


def _raw_load() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("top level is not a JSON object")
        return cfg
    except (ValueError, OSError) as e:  # JSONDecodeError is a ValueError
        # Never silently overwrite a config we could not read: park it next
        # to the original so the profiles can be recovered by hand.
        backup = p.with_name(p.name + ".corrupt")
        try:
            p.replace(backup)
            where = f"moved it to {backup}"
        except OSError:
            where = "could not move it aside"
        print(f"gity: warning: could not parse {p} ({e}); {where} and starting fresh",
              file=sys.stderr)
        return {}


def _raw_save(cfg: dict) -> None:
    _ensure_dirs()
    p = config_path()
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def load() -> dict:
    cfg = _raw_load()
    for section in ("profiles", "repos"):
        if not isinstance(cfg.get(section), dict):
            cfg[section] = {}
    return _normalize(cfg)


def _normalize(cfg: dict) -> dict:
    """Make key paths machine-local.

    Configs exported from another OS/user carry absolute paths like
    /Users/alice/.gity/keys/work_key. On this machine the keys live in
    ~/.gity/keys/<profile>_key, so remap whenever the local file exists —
    and also when the stored path doesn't (a foreign path can never work
    here). This self-heals configs imported before path rewriting existed.
    """
    kd = keys_dir()
    for name, meta in cfg.get("profiles", {}).items():
        if not isinstance(meta, dict):
            continue
        local = kd / f"{name}_key"
        stored = meta.get("key_path")
        stored_exists = isinstance(stored, str) and bool(stored) and Path(stored).expanduser().exists()
        if local.exists() or not stored_exists:
            meta["key_path"] = str(local)
            meta["public_key_path"] = str(local) + ".pub"
    return cfg


def save(cfg: dict) -> None:
    _raw_save(cfg)


def list_profiles(cfg: dict) -> dict:
    return cfg.get("profiles", {})


def get_profile(cfg: dict, name: str | None):
    meta = cfg.get("profiles", {}).get(name) if isinstance(name, str) and name else None
    return meta if isinstance(meta, dict) else None


def add_profile(cfg: dict, name: str, comment: str, email: str | None,
                key_bits: int, key_type: str = "ed25519",
                user_name: str | None = None) -> Path:
    if not valid_name(name):
        raise ValueError(
            f"invalid profile name {name!r}: use letters, digits, '.', '_' or '-' "
            "(must start with a letter or digit, max 64 characters)")
    if key_type not in KEY_TYPES:
        raise ValueError(f"unsupported key type {key_type!r} (choose from {', '.join(KEY_TYPES)})")
    profiles = cfg.setdefault("profiles", {})
    if name in profiles:
        raise KeyError(f"profile '{name}' already exists (delete it first or pick another name)")
    kdir = _ensure_dirs()
    key_path = kdir / f"{name}_key"
    pub_path = kdir / f"{name}_key.pub"
    if key_path.exists() or pub_path.exists():
        raise FileExistsError(f"key file already exists: {key_path}")
    # ssh-keygen does the actual generation so flags/comments match ssh defaults
    cmd = ["ssh-keygen", "-q", "-t", key_type, "-f", str(key_path), "-N", ""]
    if key_type == "rsa":
        cmd += ["-b", str(key_bits)]
    label = comment or email
    if label:
        cmd += ["-C", label]
    try:
        subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        raise RuntimeError("ssh-keygen not found on PATH (install OpenSSH)") from None
    except subprocess.CalledProcessError as e:
        for leftover in (key_path, pub_path):
            if leftover.exists():
                leftover.unlink()
        raise RuntimeError(f"ssh-keygen failed: {(e.stderr or '').strip() or e}") from None
    os.chmod(key_path, 0o600)
    profiles[name] = {
        "comment": comment or email or "",
        "email": email or None,
        "user_name": user_name or None,
        "key_type": key_type,
        "key_path": str(key_path),
        "public_key_path": str(pub_path),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save(cfg)
    return key_path


def _inside_keys_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(keys_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def delete_profile(cfg: dict, name: str, remove_keys: bool = True) -> None:
    prof = get_profile(cfg, name)
    if not prof:
        raise KeyError(f"no such profile: {name}")
    if remove_keys:
        for k in (prof.get("key_path"), prof.get("public_key_path")):
            if not isinstance(k, str) or not k:
                continue
            kp = Path(k).expanduser()
            # Only ever delete keys gity owns. A hand-edited config may point
            # a profile at e.g. ~/.ssh/id_ed25519; that file is not ours.
            if kp.is_file() and _inside_keys_dir(kp):
                kp.unlink()
    cfg.get("profiles", {}).pop(name, None)
    # drop repo assignments referencing it
    cfg["repos"] = {r: p for r, p in cfg.get("repos", {}).items() if p != name}
    if cfg.get("default_profile") == name:
        cfg["default_profile"] = None
    save(cfg)


def set_default(cfg: dict, name: str | None) -> None:
    if name is not None and get_profile(cfg, name) is None:
        raise KeyError(f"no such profile: {name}")
    cfg["default_profile"] = name
    save(cfg)


def _repo_key(path: str) -> str:
    """Canonical form of a repo path for lookups.

    Symlinks are resolved because git itself reports resolved paths
    (`git rev-parse --show-toplevel` answers /private/tmp/x for /tmp/x on
    macOS); comparing unresolved paths would lose the binding.
    """
    return os.path.normcase(os.path.realpath(path))


def repo_assignment(cfg: dict, repo_path: str) -> str | None:
    repos = cfg.get("repos") or {}
    if not repos:
        return None
    table = {_repo_key(r): p for r, p in repos.items()}
    # exact match first, then nearest ancestor (repo may live inside a tracked dir)
    cur = _repo_key(repo_path)
    while True:
        if cur in table:
            return table[cur]
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def set_repo_assignment(cfg: dict, repo_path: str, profile: str | None) -> None:
    if profile is not None and get_profile(cfg, profile) is None:
        raise KeyError(f"no such profile: {profile}")
    repo = os.path.realpath(repo_path)
    key = _repo_key(repo)
    repos = cfg.get("repos")
    if not isinstance(repos, dict):
        repos = cfg["repos"] = {}
    # drop any older spelling of the same directory (e.g. an unresolved symlink)
    for existing in [r for r in repos if _repo_key(r) == key]:
        del repos[existing]
    if profile is not None:
        repos[repo] = profile
    save(cfg)


def resolve_profile(cfg: dict, repo_path: str, explicit: str | None = None) -> str | None:
    """Explicit --profile wins, then repo assignment, then default.

    Assignments or defaults that point at a profile which no longer exists
    are ignored rather than half-applied.
    """
    if explicit:
        if get_profile(cfg, explicit) is None:
            raise KeyError(f"no such profile: {explicit}")
        return explicit
    assigned = repo_assignment(cfg, repo_path)
    if assigned and get_profile(cfg, assigned) is not None:
        return assigned
    default = cfg.get("default_profile")
    if default and get_profile(cfg, default) is not None:
        return default
    return None


# ------------------------------------------------- export / import ---------

def export_payload(cfg: dict) -> dict:
    """Portable snapshot: profiles + settings + key files.

    Repo assignments are machine-local and intentionally NOT exported.
    Key paths are stored as bare filenames relative to the keys dir so the
    payload imports cleanly on any OS / any username.
    """
    cfg = copy.deepcopy(cfg)
    cfg.pop("repos", None)
    files = {}
    kd = keys_dir()
    for name, meta in cfg.get("profiles", {}).items():
        if not isinstance(meta, dict):
            continue
        meta["key_path"] = f"{name}_key"
        meta["public_key_path"] = f"{name}_key.pub"
        local = kd / f"{name}_key"
        if local.exists():
            files[local.name] = base64.b64encode(local.read_bytes()).decode("ascii")
            pub = kd / f"{name}_key.pub"
            if pub.exists():
                files[pub.name] = base64.b64encode(pub.read_bytes()).decode("ascii")
    return {"version": EXPORT_VERSION, "config": cfg, "files": files}


def _validated_payload(payload: object) -> tuple[dict, dict, dict]:
    """Check an import payload before anything touches the disk.

    The file names come from the export file, i.e. from whoever produced
    it. They are written into the keys directory, so anything that is not
    a plain `<profile>_key[.pub]` name (path separators, `..`, absolute
    paths) is rejected instead of being allowed to escape that directory.
    """
    if not isinstance(payload, dict):
        raise ValueError("export payload is not a JSON object")
    version = payload.get("version")
    if version != EXPORT_VERSION:
        raise ValueError(f"unsupported export version {version!r} "
                         f"(this gity reads version {EXPORT_VERSION})")
    config = payload.get("config", {})
    files = payload.get("files", {})
    if not isinstance(config, dict) or not isinstance(files, dict):
        raise ValueError("export payload is malformed")
    profiles = config.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("export payload is malformed (profiles)")
    for name, meta in profiles.items():
        if not valid_name(name) or not isinstance(meta, dict):
            raise ValueError(f"export contains an invalid profile entry: {name!r}")
    decoded = {}
    for fname, b64 in files.items():
        if not isinstance(fname, str) or not _KEY_FILE_RE.match(fname):
            raise ValueError(f"export contains an unexpected file name: {fname!r}")
        try:
            decoded[fname] = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise ValueError(f"export contains undecodable data for {fname!r}") from None
    return config, profiles, decoded


def _write_key_file(dest: Path, data: bytes, private: bool) -> None:
    if private:
        # created 0600 from the start: no window where the key is world-readable
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(dest, 0o600)
    else:
        dest.write_bytes(data)


def import_payload(payload: dict, overwrite_keys: bool = False) -> dict:
    """Merge an exported payload into the local gity home.

    Only profiles, key files, and settings are imported. Repo assignments
    are machine-local by design and are never imported. Every key path is
    rewritten to THIS machine's ~/.gity/keys/<profile>_key, regardless of
    what the exporting machine stored, so an export made under
    /Users/alice imports correctly under /home/bob or C:/Users/bob.

    Raises ValueError if the payload is malformed.
    """
    imported, imported_profiles, files = _validated_payload(payload)
    cfg = load()
    kd = _ensure_dirs()

    keys_added, keys_kept = [], 0
    for name, data in files.items():
        dest = kd / name
        if dest.exists() and not overwrite_keys:
            keys_kept += 1
            continue
        _write_key_file(dest, data, private=not name.endswith(".pub"))
        keys_added.append(name)

    profiles_added = []
    for name, meta in imported_profiles.items():
        if name in cfg["profiles"]:
            continue
        meta = dict(meta)
        meta["key_path"] = str(kd / f"{name}_key")
        meta["public_key_path"] = str(kd / f"{name}_key.pub")
        cfg["profiles"][name] = meta
        profiles_added.append(name)

    # carry over settings like auto_detect
    for key in ("auto_detect",):
        if key in imported and key not in cfg:
            cfg[key] = bool(imported[key])

    wanted_default = imported.get("default_profile")
    if cfg.get("default_profile") is None and get_profile(cfg, wanted_default) is not None:
        cfg["default_profile"] = wanted_default

    save(cfg)
    return {
        "profiles_added": profiles_added,
        "keys_added": keys_added,
        "keys_kept": keys_kept,
        "default_profile": cfg.get("default_profile"),
    }
