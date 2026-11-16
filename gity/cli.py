"""gity — git, with SSH-key profiles for multiple accounts.

Usage:

    gity <any git command and flags> [--profile NAME]

    gity profile -i [NAME]          create a profile (new SSH key + commit identity)
    gity profile -l                 list profiles and repo bindings
    gity profile -a NAME            bind profile NAME to the current repo
    gity profile -d NAME            set the default profile
    gity profile --remove NAME      delete a profile and its keys (asks first; -y to skip)
    gity profile --delete NAME      delete a profile and its keys (no prompt)
    gity profile --setauto [on|off] auto-detect the right profile when cloning

    gity --export FILE              encrypted backup of profiles + keys
    gity --import FILE              restore a backup on this machine

`--profile NAME` (or `--profile=NAME`) anywhere in the line routes that one
command through the named profile's SSH key; `gity clone` also accepts the
short form `-p NAME`. A cloned directory is bound to the profile that cloned
it, and every later command inside a bound repo uses that profile
automatically. Everything else is passed to git untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from . import crypto as C
from . import profiles as P
from .ssh_env import run_git_with_profile, ssh_command_for
from .util import is_ssh_url, ssh_target, url_to_repo_name

# git's own options that sit before the subcommand and take a separate value
_GIT_GLOBAL_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                           "--config-env", "--attr-source", "--super-prefix"}

_CLONE_VALUE_FLAGS = {"-b", "--branch", "--depth", "-o", "--origin", "-j", "--jobs",
                      "-u", "--upload-pack", "-c", "--config",
                      "--template", "--reference", "--reference-if-able",
                      "--shallow-since", "--shallow-exclude", "--server-option",
                      "--separate-git-dir", "--filter", "--bundle-uri",
                      "--ref-format", "--revision"}
_INIT_VALUE_FLAGS = {"-b", "--initial-branch", "--template", "--separate-git-dir",
                     "--object-format", "--ref-format"}
_WORKTREE_ADD_VALUE_FLAGS = {"-b", "-B", "--reason"}


def die(msg: str, code: int = 1):
    print(f"gity: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------- arg parsing ---

def _locate_subcommand(git_args: list[str]) -> tuple[int | None, list[str]]:
    """Index of the git subcommand, plus every `-C <dir>` that precedes it."""
    dirs: list[str] = []
    i = 0
    while i < len(git_args):
        a = git_args[i]
        if a in _GIT_GLOBAL_VALUE_FLAGS:
            if a == "-C" and i + 1 < len(git_args):
                dirs.append(git_args[i + 1])
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return i, dirs
    return None, dirs


def split_profile_args(args: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Split args into (profile, git_subcommand, git_args).

    `--profile NAME` / `--profile=NAME` is recognised anywhere before a
    literal `--`. The short `-p NAME` is recognised for `clone` only: for
    other commands `-p` already belongs to git (`log -p`, `add -p`, ...).
    """
    profile = None
    git_args: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            git_args.extend(args[i:])
            break
        if a == "--profile":
            if i + 1 >= len(args):
                die("--profile requires a value")
            profile = args[i + 1]
            i += 2
            continue
        if a.startswith("--profile="):
            profile = a.split("=", 1)[1]
            i += 1
            continue
        git_args.append(a)
        i += 1

    idx, _ = _locate_subcommand(git_args)
    sub = git_args[idx] if idx is not None else None

    if sub == "clone":
        i = idx + 1
        while i < len(git_args) and git_args[i] != "--":
            if git_args[i] == "-p":
                if i + 1 >= len(git_args):
                    die("-p requires a profile name")
                profile = git_args[i + 1]
                del git_args[i:i + 2]
                continue
            i += 1

    if profile is not None and not profile:
        die("--profile requires a value")
    return profile, sub, git_args


def _positionals(args: list[str], value_flags: set[str]) -> list[str]:
    """Positional arguments of a git subcommand, skipping flags and their values."""
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a in value_flags:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        positional.append(a)
        i += 1
    return positional


def _find_clone_dest(git_args: list[str]) -> str | None:
    """Best-effort: destination dir of `git clone [opts] url [dir]`."""
    args = git_args[1:]
    positional = _positionals(args, _CLONE_VALUE_FLAGS)
    if not positional:
        return None
    if len(positional) >= 2:
        return positional[1]
    name = url_to_repo_name(positional[0])
    opts = args[:args.index("--")] if "--" in args else args
    if "--bare" in opts or "--mirror" in opts:
        name += ".git"  # git appends .git to the derived name for bare clones
    return name


def _git_root(path: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.stdout.strip() or None


def _apply_identity(repo: str, meta: dict, profile: str) -> str | None:
    """Set the repo's commit identity from a profile; return it for display."""
    email = meta.get("email")
    name = meta.get("user_name") or (profile if email else None)
    if not email and not name:
        return None
    try:
        if email:
            subprocess.run(["git", "-C", repo, "config", "user.email", email], capture_output=True)
        if name:
            subprocess.run(["git", "-C", repo, "config", "user.name", name], capture_output=True)
    except OSError:
        return None
    return f"{name} <{email}>" if email else str(name)


def _print_clone_summary(effective: str | None, key_path: str | None, rc: int, auto: bool = False) -> None:
    """Always tell the user which profile/key a clone used (or would use)."""
    tag = "auto-detected" if auto else "profile"
    if effective and key_path:
        print(f"gity: clone used {tag} '{effective}' — ssh key: {key_path} (rc={rc})")
    else:
        print(f"gity: clone used no gity profile — plain ssh config (~/.ssh) applied (rc={rc})")


# ------------------------------------------------------------ key profile ---

def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{text}{suffix}: ").strip()
    except EOFError:  # stdin closed / piped and exhausted: take the default
        print()
        val = ""
    return val or (default or "")


def cmd_profile(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="gity profile",
                                 description="Manage gity SSH-key profiles.")
    ap.add_argument("-i", "--init", nargs="?", const="__interactive__", metavar="NAME",
                    help="generate a new SSH key profile")
    ap.add_argument("-l", "--list", action="store_true", help="list profiles")
    ap.add_argument("-a", "--apply", metavar="NAME",
                    help="assign profile NAME to the current repo")
    ap.add_argument("-d", "--default", metavar="NAME",
                    help="set default profile everywhere")
    ap.add_argument("--delete", metavar="NAME", help="delete a profile and its keys (no prompt)")
    ap.add_argument("--remove", metavar="NAME", help="delete a profile and its keys (asks for confirmation)")
    ap.add_argument("--setauto", nargs="?", const="toggle", metavar="ON|OFF",
                    help="toggle auto-detect for clones: probe every profile key and bind "
                         "the first one that can read the repo being cloned")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the --remove confirmation")
    ap.add_argument("--email", help="git commit email for this profile")
    ap.add_argument("--name", help="git commit name for this profile")
    ap.add_argument("--comment", help="SSH key comment (defaults to the email)")
    ap.add_argument("--type", default="ed25519", choices=list(P.KEY_TYPES))
    ap.add_argument("--bits", type=int, default=4096, help="bits for rsa keys")
    ns = ap.parse_args(args)

    cfg = P.load()

    if ns.init:
        name = ns.init if ns.init != "__interactive__" else prompt(
            "What profile do you want to save it as").replace(" ", "-")
        if not name:
            die("profile name required")
        if not P.valid_name(name):
            die(f"invalid profile name '{name}': use letters, digits, '.', '_' or '-' "
                "(must start with a letter or digit)")
        if P.get_profile(cfg, name):
            die(f"profile '{name}' already exists")
        # --name and --email together make the whole command non-interactive
        scripted = bool(ns.name and ns.email)
        git_name = ns.name or prompt("What git name (for commits with this profile)", name)
        git_email = ns.email or prompt("What git email (for commits with this profile)", "")
        if ns.comment is not None:
            comment = ns.comment
        elif scripted:
            comment = git_email
        else:
            comment = prompt("Key comment (usually name/email)", f"{git_email or name}")
        try:
            key_path = P.add_profile(cfg, name, comment=comment, email=git_email,
                                     key_bits=ns.bits, key_type=ns.type, user_name=git_name)
        except (KeyError, ValueError, OSError, RuntimeError) as e:
            die(str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e))
        pub = P.keys_dir() / f"{name}_key.pub"
        print(f"gity: generated {ns.type} key for profile '{name}'")
        print(f"  private: {key_path}")
        print(f"  public:  {pub}")
        if git_email:
            print(f"  commit identity: {git_name} <{git_email}>")
        print("  public key (add this to your git host):")
        print("  " + pub.read_text(encoding="utf-8").strip())
        if scripted:
            if cfg.get("default_profile") != name:
                print(f"  make it the default with: gity profile -d {name}")
        elif prompt("Set as default profile? (y/N)", "n").lower() in ("y", "yes"):
            P.set_default(cfg, name)
            print(f"gity: '{name}' is now the default profile")
        return 0

    if ns.apply:
        meta = P.get_profile(cfg, ns.apply)
        if meta is None:
            die(f"no such profile: {ns.apply}")
        repo = _git_root(".") or os.getcwd()
        P.set_repo_assignment(cfg, repo, ns.apply)
        identity = _apply_identity(repo, meta, ns.apply)
        print(f"gity: repo {repo} now uses profile '{ns.apply}'"
              + (f" (git identity set to {identity})" if identity else ""))
        return 0

    if ns.default:
        if P.get_profile(cfg, ns.default) is None:
            die(f"no such profile: {ns.default}")
        P.set_default(cfg, ns.default)
        print(f"gity: default profile set to '{ns.default}'")
        return 0

    if ns.remove:
        name = ns.remove
        meta = P.get_profile(cfg, name)
        if meta is None:
            die(f"no such profile: {name}")
        repos_using = [r for r, p in cfg.get("repos", {}).items() if p == name]
        is_default = cfg.get("default_profile") == name
        print(f"gity: about to remove profile '{name}'")
        print(f"  private key: {meta.get('key_path')}")
        print(f"  repos assigned to it: {len(repos_using)}"
              + (f" ({', '.join(repos_using[:3])}{'...' if len(repos_using) > 3 else ''})"
                 if repos_using else ""))
        if is_default:
            print("  (this is your default profile)")
        if not (ns.yes or prompt("Type the profile name to confirm removal", "").strip() == name):
            print("gity: aborted")
            return 1
        try:
            P.delete_profile(cfg, name)
        except KeyError as e:
            die(str(e.args[0]))
        print(f"gity: removed profile '{name}' and its keys")
        if is_default:
            print("  (default profile unset — set a new one with: gity profile -d <name>)")
        return 0

    if ns.setauto is not None:
        val = ns.setauto
        if val == "toggle":
            cfg["auto_detect"] = not cfg.get("auto_detect")
        else:
            val = val.strip().lower()
            if val not in ("on", "off", "true", "false", "1", "0", "yes", "no"):
                die("--setauto takes on/off (or nothing to toggle)")
            cfg["auto_detect"] = val in ("on", "1", "true", "yes")
        P.save(cfg)
        state = "ON" if cfg.get("auto_detect") else "OFF"
        print(f"gity: auto-detect is now {state}")
        if cfg.get("auto_detect"):
            print("  SSH clones with no explicit --profile will probe every profile key;")
            print("  the first key that can read the repo is bound to the cloned directory")
        return 0

    if ns.delete:
        try:
            P.delete_profile(cfg, ns.delete)
        except KeyError as e:
            die(str(e.args[0]))
        print(f"gity: deleted profile '{ns.delete}' and its keys")
        return 0

    # bare `gity profile` or -l: list
    profs = P.list_profiles(cfg)
    if not profs:
        print("no profiles yet — create one with: gity profile -i")
        return 0
    print(f"{'PROFILE':<20} {'TYPE':<9} {'CREATED':<20} {'DEFAULT':<8} KEY")
    for n, meta in profs.items():
        if not isinstance(meta, dict):
            continue
        star = n == cfg.get("default_profile")
        print(f"{n + ('*' if star else ''):<20} {str(meta.get('key_type') or '?'):<9} "
              f"{str(meta.get('created_at') or '')[:19]:<20} "
              f"{'yes' if star else '':<8} {meta.get('key_path') or ''}")
    repos = cfg.get("repos", {})
    if repos:
        print("\nrepo assignments:")
        for r, p in sorted(repos.items()):
            print(f"  {str(p):<15} {r}")
    print(f"\nauto-detect on clone: {'on' if cfg.get('auto_detect') else 'off'}")
    return 0


# -------------------------------------------------------------- git proxy ---

def main(argv: list[str] | None = None) -> int:
    try:
        return _main(list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


def _transport_request(argv: list[str]) -> tuple[str, str] | None:
    """`gity --export FILE` / `gity --import FILE` (only as the first argument,
    so git's own arguments are never mistaken for it)."""
    if not argv:
        return None
    first = argv[0]
    for flag in ("--export", "--import"):
        if first == flag:
            if len(argv) < 2:
                die(f"{flag} requires a file path")
            if len(argv) > 2:
                die(f"{flag} takes exactly one file path")
            return flag, argv[1]
        if first.startswith(flag + "="):
            if len(argv) > 1:
                die(f"{flag} takes exactly one file path")
            path = first.split("=", 1)[1]
            if not path:
                die(f"{flag} requires a file path")
            return flag, path
    return None


def _main(argv: list[str]) -> int:
    transport = _transport_request(argv)
    if transport:
        flag, path = transport
        return cmd_export(path) if flag == "--export" else cmd_import(path)

    if argv == ["--version"]:
        print(f"gity version {__version__}")
        return run_git_with_profile(argv, None)

    profile, sub, git_args = split_profile_args(argv)

    if not git_args:
        print(__doc__)
        return 0

    sub_idx, c_dirs = _locate_subcommand(git_args)

    if sub == "profile" and sub_idx == 0:
        if profile is not None:
            die("--profile cannot be combined with 'gity profile'")
        return cmd_profile(git_args[1:])

    cfg = P.load()

    # honour `gity -C <dir> ...` exactly like git does
    base = os.getcwd()
    for d in c_dirs:
        base = os.path.join(base, d)
    repo_path = _git_root(base) or base

    try:
        effective = P.resolve_profile(cfg, repo_path, profile)
    except KeyError as e:
        die(str(e.args[0]))
    key_path = None
    if effective:
        key_path = (P.get_profile(cfg, effective) or {}).get("key_path")

    sub_args = git_args[sub_idx:] if sub_idx is not None else []

    if sub == "clone":
        # --setauto mode: any clone without an explicit --profile probes every
        # profile key and uses the first that can read the repo — even when a
        # default profile exists, because the default is often the wrong
        # account for a given repo.
        used_auto = False
        if profile is None and cfg.get("auto_detect"):
            detected = auto_detect_profile(cfg, sub_args)
            if detected:
                effective = detected
                used_auto = True
                key_path = (P.get_profile(cfg, effective) or {}).get("key_path")
        dest = _find_clone_dest(sub_args)
        rc = run_git_with_profile(git_args, key_path)
        _print_clone_summary(effective, key_path, rc, auto=used_auto)
        if rc == 0 and effective and dest:
            dest_abs = os.path.abspath(os.path.join(base, dest))
            if os.path.isdir(dest_abs):
                P.set_repo_assignment(cfg, dest_abs, effective)
                _apply_identity(dest_abs, P.get_profile(cfg, effective) or {}, effective)
                print(f"gity: '{dest_abs}' bound to profile '{effective}'")
            else:
                print("gity: could not locate the cloned directory — bind it with: "
                      f"gity profile -a {effective}", file=sys.stderr)
        return rc

    rc = run_git_with_profile(git_args, key_path)

    # A freshly created repo/worktree inherits the profile that created it.
    if rc == 0 and effective:
        new_dir = None
        if sub == "init":
            target = _positionals(sub_args[1:], _INIT_VALUE_FLAGS)
            new_dir = os.path.join(base, target[0]) if target else base
            new_dir = _git_root(new_dir) or new_dir
        elif sub == "worktree" and sub_args[1:2] == ["add"]:
            target = _positionals(sub_args[2:], _WORKTREE_ADD_VALUE_FLAGS)
            new_dir = os.path.join(base, target[0]) if target else None
        if new_dir and os.path.isdir(new_dir) and not P.repo_assignment(cfg, new_dir):
            new_dir = os.path.abspath(new_dir)
            P.set_repo_assignment(cfg, new_dir, effective)
            _apply_identity(new_dir, P.get_profile(cfg, effective) or {}, effective)
            print(f"gity: new repo {new_dir} bound to profile '{effective}'")
    return rc


# ------------------------------------------------------------ auto-detect ---

def _url_host(url: str) -> str | None:
    """Extract the host from a git URL (scheme or scp-style)."""
    if "://" in url:
        from urllib.parse import urlparse
        try:
            return urlparse(url).hostname
        except ValueError:
            return None
    target = ssh_target(url)
    return target[1] if target else None


def _probe_destination(url: str) -> str | None:
    """What to hand to `ssh -T` to test authentication for this remote."""
    target = ssh_target(url)
    if not target:
        return None
    user, host, port = target
    if port or not user:
        if ":" in host:  # bare IPv6 literal
            host = f"[{host}]"
        return "ssh://" + (f"{user}@" if user else "") + host + (f":{port}" if port else "")
    return host if user == "git" else f"{user}@{host}"


def probe_key_auth(host: str, key_path: str, timeout: int = 15) -> bool:
    """Try authenticating to a git host with one key (BatchMode, no prompts).

    `host` is a bare hostname (the `git` user is assumed), `user@host`, or an
    `ssh://` destination. Git hosts refuse shell access, so a *successful*
    login still exits nonzero (GitHub: 'Hi <user>! You've successfully
    authenticated', exit 1). ssh itself exits 255 only when the connection or
    the authentication failed, which is what we key on.
    """
    dest = host if ("@" in host or "://" in host) else f"git@{host}"
    cmd = ["ssh", "-i", key_path,
           "-o", "IdentitiesOnly=yes",
           "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", f"ConnectTimeout={timeout}",
           "-T", dest]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout + 5)
    except (subprocess.TimeoutExpired, OSError):
        return False
    text = (proc.stdout or "") + (proc.stderr or "")
    if "Permission denied" in text:
        return False
    return (proc.returncode != 255
            or "successfully authenticated" in text
            or "Welcome to" in text)


def probe_repo_access(url: str, key_path: str, timeout: int = 15) -> bool:
    """Check whether this key can actually read the repo (not just the host).

    A key may authenticate to git@github.com as one account while the repo
    lives under another account, so host-level auth is not enough.
    """
    try:
        ssh_cmd = ssh_command_for(key_path, ["-o", f"ConnectTimeout={timeout}"])
    except FileNotFoundError:
        return False
    env = dict(os.environ, GIT_SSH_COMMAND=ssh_cmd, GIT_SSH_VARIANT="ssh",
               GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(["git", "ls-remote", url, "HEAD"],
                              env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout + 5)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def auto_detect_profile(cfg: dict, git_args: list[str]) -> str | None:
    """--setauto mode: find the profile key that can access this repo.

    `git_args` is the clone command starting at `clone`.
    """
    positional = _positionals(git_args[1:], _CLONE_VALUE_FLAGS)
    if not positional:
        return None
    url = positional[0]
    # Keys only matter for SSH remotes; https:// uses credential helpers.
    dest = _probe_destination(url) if is_ssh_url(url) else None
    if not dest:
        return None
    candidates = []
    for name, meta in cfg.get("profiles", {}).items():
        key_path = meta.get("key_path") if isinstance(meta, dict) else None
        if key_path and Path(key_path).expanduser().exists():
            candidates.append((name, key_path))
    if not candidates:
        return None
    # Cheap host-level auth first to skip dead keys, then verify the key can
    # actually read this specific repo.
    print(f"gity: auto-detect: probing {len(candidates)} key(s) against {url} ...")
    for name, key_path in candidates:
        if probe_key_auth(dest, key_path) and probe_repo_access(url, key_path):
            print(f"gity: auto-detect: key for profile '{name}' has access to this repo")
            return name
    print("gity: auto-detect: no key can access this repo; falling back to default resolution",
          file=sys.stderr)
    return None


# --------------------------------------------------- export / import ---

def cmd_export(path: str) -> int:
    cfg = P.load()
    if not P.list_profiles(cfg):
        die("nothing to export — no profiles found")
    payload = P.export_payload(cfg)
    plaintext = json.dumps(payload).encode("utf-8")
    blob = C.encrypt_bytes(C.ask_passphrase(), plaintext)
    out = Path(path).expanduser()
    if not out.suffix:
        out = out.with_suffix(".gity")
    try:
        # 0600 from creation: the file holds private keys (encrypted)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.chmod(out, 0o600)
    except OSError as e:
        die(f"cannot write {out}: {e.strerror or e}")
    n_profs = len(P.list_profiles(cfg))
    n_files = len(payload.get("files", {}))
    print(f"gity: exported {n_profs} profile(s), {n_files} key file(s) -> {out}")
    print("     keep this file safe: it contains your private keys (passphrase-protected)")
    return 0


def cmd_import(path: str) -> int:
    src = Path(path).expanduser()
    if not src.is_file():
        die(f"file not found: {src}")
    try:
        blob = src.read_bytes()
    except OSError as e:
        die(f"cannot read {src}: {e.strerror or e}")
    if not C.is_export_blob(blob):
        die(f"{src} is not a gity export file")
    plaintext = None
    for _ in range(3):
        try:
            plaintext = C.decrypt_bytes(C.ask_decrypt_passphrase(), blob)
            break
        except C.WrongPassphrase:
            print("gity: wrong passphrase (or the file is corrupt), try again", file=sys.stderr)
        except ValueError as e:
            die(str(e))
    if plaintext is None:
        die("wrong passphrase after 3 attempts")
    try:
        payload = json.loads(plaintext.decode("utf-8"))
        summary = P.import_payload(payload)
    except ValueError as e:  # bad JSON / bad unicode / malformed payload
        die(f"cannot import {src}: {e}")
    print(f"gity: imported {len(summary.get('profiles_added', []))} profile(s), "
          f"{len(summary.get('keys_added', []))} new key file(s)")
    if summary["keys_kept"]:
        print(f"     ({summary['keys_kept']} key file(s) already existed and were kept)")
    if summary.get("default_profile"):
        print(f"     default profile: {summary['default_profile']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
