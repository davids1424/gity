"""Tests for gity profile management and git passthrough.

Run with:  python3 -m pytest tests/ -v   (or python3 -m unittest discover tests)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Each test case gets an isolated GITY_HOME via env; profiles module reads it
# dynamically, so we set it per-test.

from gity import profiles as P  # noqa: E402
from gity.util import url_to_repo_name, looks_like_scp  # noqa: E402
from gity.cli import split_profile_args, _find_clone_dest  # noqa: E402


class GityTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GITY_HOME"] = self.tmp.name
        self.home = Path(self.tmp.name)
        self.home.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("GITY_HOME", None)
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


class TestUtil(GityTestBase):
    def test_url_to_repo_name_https(self):
        self.assertEqual(url_to_repo_name("https://github.com/u/repo.git"), "repo")

    def test_url_to_repo_name_scp(self):
        self.assertEqual(url_to_repo_name("git@github.com:u/repo.git"), "repo")

    def test_url_to_repo_name_no_ext(self):
        self.assertEqual(url_to_repo_name("https://github.com/u/repo"), "repo")

    def test_looks_like_scp(self):
        self.assertTrue(looks_like_scp("git@github.com:u/r.git"))
        self.assertFalse(looks_like_scp("https://github.com/u/r.git"))


class TestSplitArgs(GityTestBase):
    def test_profile_before_subcommand(self):
        p, sub, rest = split_profile_args(["--profile", "personal", "clone", "url"])
        self.assertEqual(p, "personal")
        self.assertEqual(sub, "clone")
        self.assertEqual(rest, ["clone", "url"])

    def test_profile_equals_form(self):
        p, sub, rest = split_profile_args(["--profile=work", "status"])
        self.assertEqual(p, "work")
        self.assertEqual(rest, ["status"])

    def test_profile_after_subcommand(self):
        p, sub, rest = split_profile_args(["clone", "--profile", "personal", "url"])
        self.assertEqual(p, "personal")
        self.assertEqual(rest, ["clone", "url"])

    def test_no_profile(self):
        p, sub, rest = split_profile_args(["push", "origin", "main"])
        self.assertIsNone(p)
        self.assertEqual(sub, "push")

    def test_missing_value_dies(self):
        with self.assertRaises(SystemExit):
            split_profile_args(["--profile"])


class TestCloneDest(GityTestBase):
    def test_https_url(self):
        self.assertEqual(_find_clone_dest(["clone", "https://github.com/u/repo.git"]), "repo")

    def test_scp_url(self):
        self.assertEqual(_find_clone_dest(["clone", "git@github.com:u/repo.git"]), "repo")

    def test_explicit_dir(self):
        self.assertEqual(_find_clone_dest(["clone", "url", "mydir"]), "mydir")

    def test_flags_with_values(self):
        self.assertEqual(_find_clone_dest(["clone", "-b", "main", "https://x/repo.git"]), "repo")
        self.assertEqual(_find_clone_dest(["clone", "--depth", "1", "https://x/repo.git"]), "repo")

    def test_boolean_flags(self):
        self.assertEqual(_find_clone_dest(["clone", "--quiet", "https://x/y.git"]), "y")

    def test_bare_clone_keeps_git_suffix(self):
        # `git clone --bare https://x/y.git` creates ./y.git, not ./y
        self.assertEqual(_find_clone_dest(["clone", "--bare", "https://x/y.git"]), "y.git")
        self.assertEqual(_find_clone_dest(["clone", "--mirror", "https://x/y"]), "y.git")
        self.assertEqual(_find_clone_dest(["clone", "--bare", "https://x/y.git", "z"]), "z")

    def test_more_value_flags(self):
        self.assertEqual(_find_clone_dest(["clone", "--filter", "blob:none", "https://x/r.git"]), "r")
        self.assertEqual(_find_clone_dest(["clone", "-c", "core.autocrlf=false", "https://x/r.git"]), "r")
        self.assertEqual(_find_clone_dest(["clone", "--depth=1", "https://x/r.git"]), "r")

    def test_dest_is_second_positional(self):
        self.assertEqual(_find_clone_dest(["clone", "url", "mydir", "--depth", "1"]), "mydir")

    def test_no_url(self):
        self.assertIsNone(_find_clone_dest(["clone", "--bare"]))


class TestProfiles(GityTestBase):
    def test_add_and_get(self):
        cfg = P.load()
        # don't actually run ssh-keygen in unit tests — write keys manually
        P.save(cfg)
        cfg["profiles"]["work"] = {
            "email": "w@example.com", "comment": "work", "key_type": "ed25519",
            "key_path": str(self.home / "keys" / "work_key"),
            "public_key_path": str(self.home / "keys" / "work_key.pub"),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        P.save(cfg)
        cfg2 = P.load()
        self.assertIsNotNone(P.get_profile(cfg2, "work"))
        self.assertEqual(cfg2["profiles"]["work"]["email"], "w@example.com")

    def test_duplicate_rejected(self):
        cfg = P.load()
        cfg["profiles"]["a"] = {"key_path": "x"}
        P.save(cfg)
        with self.assertRaises(KeyError):
            P.add_profile(cfg, "a", comment="c", email=None, key_bits=2048)

    def test_set_default(self):
        cfg = P.load()
        cfg["profiles"]["a"] = {"key_path": "x"}
        P.save(cfg)
        P.set_default(cfg, "a")
        self.assertEqual(P.load()["default_profile"], "a")
        with self.assertRaises(KeyError):
            P.set_default(cfg, "nope")

    def test_repo_assignment_nearest_ancestor(self):
        cfg = P.load()
        cfg["profiles"]["p"] = {"key_path": "x"}
        parent = tempfile.mkdtemp()
        child = os.path.join(parent, "sub", "deeper")
        os.makedirs(child)
        P.set_repo_assignment(cfg, parent, "p")
        self.assertEqual(P.repo_assignment(cfg, child), "p")
        self.assertIsNone(P.repo_assignment(cfg, tempfile.mkdtemp()))

    def test_resolve_order(self):
        cfg = P.load()
        cfg["profiles"]["work"] = {"key_path": "x"}
        cfg["profiles"]["personal"] = {"key_path": "y"}
        P.save(cfg)
        repo = tempfile.mkdtemp()
        # default only
        P.set_default(cfg, "work")
        self.assertEqual(P.resolve_profile(cfg, repo), "work")
        # repo assignment overrides default
        P.set_repo_assignment(cfg, repo, "personal")
        self.assertEqual(P.resolve_profile(cfg, repo), "personal")
        # explicit overrides everything
        self.assertEqual(P.resolve_profile(cfg, repo, "work"), "work")

    def test_delete_profile_cleans_repos(self):
        cfg = P.load()
        cfg["profiles"]["gone"] = {"key_path": str(self.home / "k"), "public_key_path": str(self.home / "k.pub")}
        P.save(cfg)
        repo = tempfile.mkdtemp()
        P.set_repo_assignment(cfg, repo, "gone")
        P.set_default(cfg, "gone")
        P.delete_profile(cfg, "gone")
        cfg2 = P.load()
        self.assertNotIn("gone", cfg2["profiles"])
        self.assertNotIn(repo, cfg2["repos"])
        self.assertIsNone(cfg2.get("default_profile"))


class TestGitPassthrough(GityTestBase):
    def test_passthrough_exit_code_and_output(self):
        """gity proxies real git: exit codes and stdout behave like git."""
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        # running an invalid git flag through run_git_with_profile should fail like git does
        from gity.ssh_env import run_git_with_profile
        rc = run_git_with_profile(["this-is-not-a-git-command"], None, cwd=repo, capture=True)
        self.assertNotEqual(rc, 0)

    def test_git_ssh_command_injection(self):
        from gity.ssh_env import ssh_command_for
        key = self.home / "keys" / "t_key"
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "test"],
                       check=True, capture_output=True)
        cmd = ssh_command_for(str(key))
        self.assertIn(str(key), cmd)
        self.assertIn("IdentitiesOnly=yes", cmd)


class TestEndToEndProfileFlow(GityTestBase):
    """Full flow with real ssh-keygen: create profile, verify files on disk."""

    def test_profile_init_flow(self):
        cfg = P.load()
        key_path = P.add_profile(cfg, "work", comment="work@example.com", email=None,
                                 key_bits=2048, key_type="ed25519")
        self.assertTrue(key_path.exists())
        self.assertTrue((P.keys_dir() / "work_key.pub").exists())
        self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
        cfg2 = P.load()
        self.assertIn("work", cfg2["profiles"])
        self.assertEqual(cfg2["profiles"]["work"]["email"], None)

    def test_config_json_shape(self):
        cfg = P.load()
        cfg["profiles"]["p"] = {"key_path": "/x"}
        cfg["repos"] = {"/some/repo": "p"}
        cfg["default_profile"] = "p"
        P.save(cfg)
        on_disk = json.loads((self.home / "config.json").read_text())
        self.assertEqual(on_disk["default_profile"], "p")
        self.assertIn("profiles", on_disk)
        self.assertIn("repos", on_disk)


class TestProfileIdentity(GityTestBase):
    def test_add_profile_stores_user_name_and_email(self):
        cfg = P.load()
        P.add_profile(cfg, "work", comment="w@x", email="w@example.com",
                      key_bits=2048, user_name="Ada Work")
        cfg2 = P.load()
        p = cfg2["profiles"]["work"]
        self.assertEqual(p["user_name"], "Ada Work")
        self.assertEqual(p["email"], "w@example.com")
        # key comment includes the email
        pub = (Path(self.tmp.name) / "keys" / "work_key.pub").read_text()
        self.assertIn("w@x", pub)


class TestExportImport(GityTestBase):
    def test_encrypt_decrypt_roundtrip(self):
        from gity import crypto as C
        blob = C.encrypt_bytes("hunter2", b'{"hello": "world"}')
        self.assertNotIn(b"hello", blob)
        self.assertEqual(C.decrypt_bytes("hunter2", blob), b'{"hello": "world"}')

    def test_wrong_passphrase_fails(self):
        from gity import crypto as C
        blob = C.encrypt_bytes("right", b"secret")
        with self.assertRaises(Exception):
            C.decrypt_bytes("wrong", blob)

    def test_bad_header(self):
        from gity import crypto as C
        with self.assertRaises(ValueError):
            C.decrypt_bytes("x", b"NOPE" + b"0" * 40)

    def test_export_import_roundtrip(self):
        import json as _json
        from gity import crypto as C
        # build a source gity home with a real key
        key_path = P.add_profile(P.load(), "work", comment="w@x", email="w@example.com",
                                 key_bits=2048, key_type="ed25519")
        P.set_default(P.load(), "work")
        repo = tempfile.mkdtemp()
        cfg = P.load()
        P.set_repo_assignment(cfg, repo, "work")

        payload = P.export_payload(P.load())
        plaintext = _json.dumps(payload).encode()
        blob = C.encrypt_bytes("pw123", plaintext)
        # repo assignments are machine-local: not in the export
        self.assertNotIn("repos", payload["config"])

        # simulate a DIFFERENT machine: new GITY_HOME + different username
        new_home = tempfile.mkdtemp()
        old_env = os.environ.get("GITY_HOME")
        fake_home = tempfile.mkdtemp()  # stands in for /Users/otheruser
        os.environ["GITY_HOME"] = new_home
        old_expand = Path.expanduser
        try:
            self.assertFalse(P.list_profiles(P.load()))
            decoded = _json.loads(C.decrypt_bytes("pw123", blob).decode())
            summary = P.import_payload(decoded)
            cfg2 = P.load()
            self.assertIn("work", cfg2["profiles"])
            self.assertEqual(cfg2["default_profile"], "work")
            # repos are machine-local: not imported
            self.assertEqual(cfg2["repos"], {})
            # key path rewritten to THIS machine's keys dir
            self.assertEqual(cfg2["profiles"]["work"]["key_path"],
                             str(Path(new_home) / "keys" / "work_key"))
            # no stale mac path anywhere
            for meta in cfg2["profiles"].values():
                self.assertNotIn("/Users/", meta["key_path"])
            # private key restored with 0600
            k = Path(new_home) / "keys" / "work_key"
            self.assertTrue(k.exists())
            self.assertEqual(os.stat(k).st_mode & 0o777, 0o600)
            self.assertEqual(len(summary["profiles_added"]), 1)
        finally:
            if old_env:
                os.environ["GITY_HOME"] = old_env

    def test_import_merges_without_overwrite(self):
        import json as _json
        from gity import crypto as C
        cfg = P.load()
        P.add_profile(cfg, "existing", comment="c", email=None, key_bits=2048)
        P.save(cfg)
        # payload references a *different* profile
        payload = {"version": 1,
                   "config": {"profiles": {"other": {"key_path": "/k/other_key"}},
                               "repos": {}, "default_profile": "other"},
                   "files": {"other_key": "ZmFrZQ==", "other_key.pub": "ZmFrZQ=="}}
        summary = P.import_payload(payload)
        cfg2 = P.load()
        self.assertIn("other", cfg2["profiles"])
        self.assertIn("existing", cfg2["profiles"])  # untouched
        self.assertEqual(cfg2["default_profile"], "other")


class TestRemove(GityTestBase):
    def test_remove_is_registered_alias(self):
        from gity.cli import cmd_profile
        import io, contextlib
        cfg = P.load()
        P.add_profile(cfg, "temp", comment="c", email=None, key_bits=2048)
        # wrong confirmation -> aborted, profile kept
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("wrongname\n")
        try:
            with contextlib.redirect_stdout(buf):
                rc = cmd_profile(["--remove", "temp"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 1)
        self.assertIn("temp", P.load()["profiles"])
        # correct confirmation -> removed
        old = sys.stdin
        sys.stdin = io.StringIO("temp\n")
        try:
            with contextlib.redirect_stdout(buf):
                rc = cmd_profile(["--remove", "temp"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertNotIn("temp", P.load()["profiles"])

    def test_remove_unknown_profile_dies(self):
        from gity.cli import cmd_profile
        with self.assertRaises(SystemExit):
            cmd_profile(["--remove", "ghost"])

    def test_remove_shows_key_path_and_repos(self):
        from gity.cli import cmd_profile
        import io, contextlib
        cfg = P.load()
        P.add_profile(cfg, "shown", comment="c", email=None, key_bits=2048)
        repo = tempfile.mkdtemp()
        P.set_repo_assignment(cfg, repo, "shown")
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("shown\n")
        try:
            with contextlib.redirect_stdout(buf):
                cmd_profile(["--remove", "shown"])
        finally:
            sys.stdin = old
        out = buf.getvalue()
        self.assertIn("keys/shown_key", out)
        self.assertIn(repo, out)


class TestSetAuto(GityTestBase):
    def test_toggle_persists(self):
        from gity.cli import cmd_profile
        import io, contextlib
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(buf):
                cmd_profile(["--setauto"])
        finally:
            sys.stdin = old
        self.assertTrue(P.load().get("auto_detect"))
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(buf):
                cmd_profile(["--setauto"])
        finally:
            sys.stdin = old
        self.assertFalse(P.load().get("auto_detect"))
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(buf):
                cmd_profile(["--setauto", "on"])
        finally:
            sys.stdin = old
        self.assertTrue(P.load().get("auto_detect"))
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(buf):
                cmd_profile(["--setauto", "off"])
        finally:
            sys.stdin = old
        self.assertFalse(P.load().get("auto_detect"))

    def test_url_host_extraction(self):
        from gity.cli import _url_host
        self.assertEqual(_url_host("git@github.com:u/r.git"), "github.com")
        self.assertEqual(_url_host("https://gitlab.com/u/r.git"), "gitlab.com")
        self.assertIsNone(_url_host("/some/local/path"))

    def test_probe_success_matches_github_greeting(self):
        from gity.cli import probe_key_auth
        import gity.cli as cli
        key = self.home / "keys" / "probe_key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("fake")
        orig = cli.subprocess.run
        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "Hi octocat! You've successfully authenticated, but GitHub does not provide shell access."
        called = {}
        def fake_run(cmd, **kwargs):
            called["cmd"] = cmd
            class R:
                returncode = 1; stdout = ""
                stderr = "Hi octocat! You've successfully authenticated."
            return R()
        cli.subprocess.run = fake_run
        try:
            self.assertTrue(cli.probe_key_auth("github.com", str(key)))
            self.assertIn("IdentitiesOnly=yes", " ".join(called["cmd"]))
            self.assertIn("BatchMode=yes", " ".join(called["cmd"]))
        finally:
            cli.subprocess.run = orig

    def test_probe_failure(self):
        import gity.cli as cli
        orig = cli.subprocess.run
        cli.subprocess.run = lambda cmd, **kw: type("R", (), {"returncode": 1, "stdout": "", "stderr": "Permission denied (publickey)"})()
        try:
            self.assertFalse(cli.probe_key_auth("github.com", "/nonexistent/key"))
        finally:
            cli.subprocess.run = orig

    def test_auto_detect_picks_first_passing_key(self):
        import gity.cli as cli
        cfg = P.load()
        P.add_profile(cfg, "wrongone", comment="c", email=None, key_bits=2048)
        P.add_profile(cfg, "rightone", comment="c", email=None, key_bits=2048)
        orig, orig_repo = cli.probe_key_auth, cli.probe_repo_access
        calls = []
        def fake(host, key, timeout=15):
            calls.append(key)
            return "wrongone" not in key
        cli.probe_key_auth = fake
        cli.probe_repo_access = lambda url, key, timeout=15: True  # no network in tests
        try:
            got = cli.auto_detect_profile(cfg, ["clone", "git@github.com:u/r.git"])
        finally:
            cli.probe_key_auth, cli.probe_repo_access = orig, orig_repo
        self.assertEqual(got, "rightone")
        self.assertEqual(calls[0], str(P.keys_dir() / "wrongone_key"))

    def test_auto_detect_requires_repo_access_not_just_host_auth(self):
        """A key that logs in to the host but cannot read the repo is skipped."""
        import gity.cli as cli
        cfg = P.load()
        P.add_profile(cfg, "hostonly", comment="c", email=None, key_bits=2048)
        P.add_profile(cfg, "owner", comment="c", email=None, key_bits=2048)
        orig, orig_repo = cli.probe_key_auth, cli.probe_repo_access
        cli.probe_key_auth = lambda host, key, timeout=15: True
        cli.probe_repo_access = lambda url, key, timeout=15: "owner" in key
        try:
            got = cli.auto_detect_profile(cfg, ["clone", "git@github.com:u/r.git"])
        finally:
            cli.probe_key_auth, cli.probe_repo_access = orig, orig_repo
        self.assertEqual(got, "owner")

    def test_auto_detect_finds_url_after_value_flags(self):
        """Regression: `clone -b main URL` probed 'main' instead of the URL."""
        import gity.cli as cli
        cfg = P.load()
        P.add_profile(cfg, "only", comment="c", email=None, key_bits=2048)
        seen = {}
        orig, orig_repo = cli.probe_key_auth, cli.probe_repo_access
        cli.probe_key_auth = lambda host, key, timeout=15: seen.setdefault("host", host) or True
        cli.probe_repo_access = lambda url, key, timeout=15: seen.setdefault("url", url) or True
        try:
            got = cli.auto_detect_profile(
                cfg, ["clone", "-b", "main", "--depth", "1", "git@github.com:u/r.git", "dest"])
        finally:
            cli.probe_key_auth, cli.probe_repo_access = orig, orig_repo
        self.assertEqual(got, "only")
        self.assertEqual(seen["host"], "github.com")
        self.assertEqual(seen["url"], "git@github.com:u/r.git")

    def test_auto_detect_skips_non_ssh_urls(self):
        """https remotes don't use SSH keys, so no key may be 'detected'."""
        import gity.cli as cli
        cfg = P.load()
        P.add_profile(cfg, "only", comment="c", email=None, key_bits=2048)
        orig, orig_repo = cli.probe_key_auth, cli.probe_repo_access
        cli.probe_key_auth = cli.probe_repo_access = lambda *a, **k: self.fail("probed")
        try:
            got = cli.auto_detect_profile(cfg, ["clone", "https://github.com/u/r.git"])
        finally:
            cli.probe_key_auth, cli.probe_repo_access = orig, orig_repo
        self.assertIsNone(got)

    def test_probe_destination_honours_user_and_port(self):
        from gity.cli import _probe_destination
        self.assertEqual(_probe_destination("git@github.com:u/r.git"), "github.com")
        self.assertEqual(_probe_destination("alice@host.example:r.git"), "alice@host.example")
        self.assertEqual(_probe_destination("ssh://git@host.example:2222/u/r.git"),
                         "ssh://git@host.example:2222")
        self.assertIsNone(_probe_destination("https://github.com/u/r.git"))


class TestCrossPlatformPaths(GityTestBase):
    def test_mac_export_imports_on_linux(self):
        """Export made under /Users/alice must work under /home/anything."""
        import json as _json
        from gity import crypto as C
        cfg = P.load()
        P.add_profile(cfg, "work", comment="w", email="w@example.com", key_bits=2048)
        P.save(cfg)
        payload = P.export_payload(P.load())
        # simulate the mac's stored paths in the payload config
        payload["config"]["profiles"]["work"]["key_path"] = "/Users/alice/.gity/keys/work_key"
        blob = C.encrypt_bytes("pw", _json.dumps(payload).encode())

        new_home = tempfile.mkdtemp()
        old_env = os.environ.get("GITY_HOME")
        os.environ["GITY_HOME"] = new_home
        try:
            decoded = _json.loads(C.decrypt_bytes("pw", blob).decode())
            P.import_payload(decoded)
            cfg2 = P.load()
            kp = cfg2["profiles"]["work"]["key_path"]
            self.assertTrue(kp.startswith(str(Path(new_home))))
            self.assertNotIn("/Users/alice", kp)
            self.assertTrue(Path(kp).exists())
        finally:
            if old_env:
                os.environ["GITY_HOME"] = old_env

    def test_load_self_heals_stale_paths(self):
        """A config carrying foreign absolute paths gets remapped on load."""
        cfg = P.load()
        P.add_profile(cfg, "heal", comment="c", email=None, key_bits=2048)
        P.save(cfg)
        # corrupt the stored path to a foreign mac path
        cfg2 = P.load()
        cfg2["profiles"]["heal"]["key_path"] = "/Users/olduser/.gity/keys/heal_key"
        P.save(cfg2)
        cfg3 = P.load()
        self.assertEqual(cfg3["profiles"]["heal"]["key_path"],
                         str(P.keys_dir() / "heal_key"))
        self.assertTrue(Path(cfg3["profiles"]["heal"]["key_path"]).exists())


class TestCloneSummary(GityTestBase):
    def test_summary_line_profiles(self):
        from gity.cli import _print_clone_summary
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_clone_summary("work", "/keys/work_key", 0, auto=False)
        self.assertIn("profile 'work'", buf.getvalue())
        self.assertIn("/keys/work_key", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_clone_summary("personal", "/keys/p_key", 0, auto=True)
        self.assertIn("auto-detected 'personal'", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_clone_summary(None, None, 0)
        self.assertIn("no gity profile", buf.getvalue())
        self.assertIn("~/.ssh", buf.getvalue())

    def test_auto_detect_beats_default_profile(self):
        """Regression: setauto must probe even when a default profile exists."""
        import gity.cli as cli
        cfg = P.load()
        P.add_profile(cfg, "defaultwrong", comment="c", email=None, key_bits=2048)
        P.add_profile(cfg, "rightkey", comment="c", email=None, key_bits=2048)
        P.set_default(cfg, "defaultwrong")
        orig, orig_repo = cli.probe_key_auth, cli.probe_repo_access
        cli.probe_key_auth = lambda host, key, timeout=15: "rightkey" in key
        cli.probe_repo_access = lambda url, key, timeout=15: True  # no network in tests
        try:
            got = cli.auto_detect_profile(cfg, ["clone", "git@github.com:u/r.git"])
        finally:
            cli.probe_key_auth, cli.probe_repo_access = orig, orig_repo
        self.assertEqual(got, "rightkey")


class TestImportSummaryPrint(GityTestBase):
    def test_import_summary_never_keys_missing_field(self):
        """Regression v0.2.6: cmd_import crashed with KeyError: 'repos_added'."""
        import io, contextlib
        from gity.cli import cmd_import
        import json as _json
        from gity import crypto as C
        P.add_profile(P.load(), "w1", comment="c", email="w@example.com", key_bits=2048)
        payload = P.export_payload(P.load())
        blob = C.encrypt_bytes("pw", _json.dumps(payload).encode())
        src = Path(self.tmp.name) / "export.gity"
        src.write_bytes(blob)
        buf = io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO("pw\n")
        # import into a FRESH machine (new GITY_HOME), like the real flow
        new_home = tempfile.mkdtemp()
        os.environ["GITY_HOME"] = new_home
        try:
            with contextlib.redirect_stdout(buf):
                rc = cmd_import(str(src))
        finally:
            sys.stdin = old
            os.environ["GITY_HOME"] = self.tmp.name
        self.assertEqual(rc, 0)
        self.assertIn("imported 1 profile(s)", buf.getvalue())
        self.assertIn("2 new key file(s)", buf.getvalue())  # private + .pub


class TestUrlParsing(GityTestBase):
    def test_repo_name_edge_cases(self):
        self.assertEqual(url_to_repo_name("host:repo.git"), "repo")
        self.assertEqual(url_to_repo_name("https://x/u/repo/"), "repo")
        self.assertEqual(url_to_repo_name("https://x/u/repo/.git"), "repo")
        self.assertEqual(url_to_repo_name("/srv/git/repo.git"), "repo")
        self.assertEqual(url_to_repo_name("C:\\src\\repo"), "repo")

    def test_scp_detection_edge_cases(self):
        self.assertTrue(looks_like_scp("git@host:/abs/path/r.git"))
        self.assertTrue(looks_like_scp("host:r.git"))
        self.assertFalse(looks_like_scp("./local:dir"))
        self.assertFalse(looks_like_scp("/abs/local:dir"))
        self.assertFalse(looks_like_scp("C:\\src\\repo"))
        self.assertFalse(looks_like_scp("ssh://git@host/r.git"))

    def test_is_ssh_url(self):
        from gity.util import is_ssh_url
        self.assertTrue(is_ssh_url("git@github.com:u/r.git"))
        self.assertTrue(is_ssh_url("ssh://git@github.com/u/r.git"))
        self.assertFalse(is_ssh_url("https://github.com/u/r.git"))
        self.assertFalse(is_ssh_url("/srv/git/r.git"))


class TestArgRouting(GityTestBase):
    def test_short_p_selects_profile_for_clone(self):
        p, sub, rest = split_profile_args(["clone", "url", "-p", "work"])
        self.assertEqual((p, sub, rest), ("work", "clone", ["clone", "url"]))
        p, sub, rest = split_profile_args(["clone", "-p", "work", "url", "dir"])
        self.assertEqual((p, rest), ("work", ["clone", "url", "dir"]))

    def test_short_p_is_left_to_git_elsewhere(self):
        """`-p` means --patch for log/add/stash...: gity must not eat it."""
        for args in (["log", "-p", "-3"], ["add", "-p"], ["stash", "-p"]):
            p, sub, rest = split_profile_args(list(args))
            self.assertIsNone(p)
            self.assertEqual(rest, args)

    def test_subcommand_found_after_git_global_options(self):
        p, sub, rest = split_profile_args(["-C", "/x", "-c", "a=b", "clone", "url", "-p", "w"])
        self.assertEqual(sub, "clone")
        self.assertEqual(p, "w")
        self.assertEqual(rest, ["-C", "/x", "-c", "a=b", "clone", "url"])

    def test_nothing_is_consumed_after_double_dash(self):
        args = ["checkout", "--", "--profile", "x"]
        p, sub, rest = split_profile_args(list(args))
        self.assertIsNone(p)
        self.assertEqual(rest, args)

    def test_empty_profile_value_dies(self):
        with self.assertRaises(SystemExit):
            split_profile_args(["--profile=", "status"])

    def test_transport_flags_only_as_first_argument(self):
        from gity.cli import _transport_request
        self.assertEqual(_transport_request(["--export", "f"]), ("--export", "f"))
        self.assertEqual(_transport_request(["--import=f"]), ("--import", "f"))
        self.assertIsNone(_transport_request(["log", "--export", "f"]))
        with self.assertRaises(SystemExit):
            _transport_request(["--export"])


class TestRegressions(GityTestBase):
    def _fake_profile(self, name, **meta):
        """Register a profile backed by a placeholder key file (no ssh-keygen)."""
        kd = P.keys_dir()
        kd.mkdir(parents=True, exist_ok=True)
        key = kd / f"{name}_key"
        key.write_text("placeholder")
        os.chmod(key, 0o600)
        cfg = P.load()
        cfg["profiles"][name] = dict({"key_path": str(key)}, **meta)
        P.save(cfg)
        return key

    def _run(self, argv):
        import contextlib, io
        from gity.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _source_repo(self):
        src = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=src, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "-c", "commit.gpgsign=false",
                        "commit", "-q", "--allow-empty", "-m", "init"], cwd=src, check=True)
        return src

    def test_clone_binds_repo_and_sets_identity(self):
        self._fake_profile("work", email="w@example.com", user_name="Work Name")
        P.set_default(P.load(), "work")
        work = tempfile.mkdtemp()
        rc, out, _ = self._run(["-C", work, "clone", "-q", self._source_repo(), "dst"])
        self.assertEqual(rc, 0)
        dst = os.path.join(work, "dst")
        self.assertEqual(P.repo_assignment(P.load(), dst), "work")
        self.assertIn("bound to profile 'work'", out)
        self.assertEqual(self.git("config", "user.email", cwd=dst).stdout.strip(), "w@example.com")
        self.assertEqual(self.git("config", "user.name", cwd=dst).stdout.strip(), "Work Name")

    def test_clone_binding_survives_git_resolving_symlinks(self):
        """Regression: clone stored /tmp/x while git reports /private/tmp/x."""
        self._fake_profile("work")
        P.set_default(P.load(), "work")
        real = tempfile.mkdtemp()
        link = os.path.join(tempfile.mkdtemp(), "link")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        rc, _, _ = self._run(["-C", link, "clone", "-q", self._source_repo(), "dst"])
        self.assertEqual(rc, 0)
        toplevel = self.git("rev-parse", "--show-toplevel", cwd=os.path.join(link, "dst")).stdout.strip()
        cfg = P.load()
        self.assertEqual(P.repo_assignment(cfg, toplevel), "work")
        self.assertEqual(P.repo_assignment(cfg, os.path.join(link, "dst")), "work")

    def test_clone_without_any_profile_binds_nothing(self):
        """Regression: printed "bound to profile 'None'"."""
        work = tempfile.mkdtemp()
        rc, out, _ = self._run(["-C", work, "clone", "-q", self._source_repo(), "dst"])
        self.assertEqual(rc, 0)
        self.assertNotIn("None", out)
        self.assertIn("no gity profile", out)
        self.assertEqual(P.load()["repos"], {})

    def test_init_binds_the_new_directory_not_the_cwd(self):
        self._fake_profile("work", email="w@example.com", user_name="W")
        P.set_default(P.load(), "work")
        work = tempfile.mkdtemp()
        rc, out, _ = self._run(["-C", work, "init", "-q", "newrepo"])
        self.assertEqual(rc, 0)
        cfg = P.load()
        self.assertEqual(len(cfg["repos"]), 1)
        self.assertEqual(os.path.basename(next(iter(cfg["repos"]))), "newrepo")
        self.assertEqual(self.git("config", "user.email",
                                  cwd=os.path.join(work, "newrepo")).stdout.strip(), "w@example.com")

    def test_missing_key_file_is_a_clean_error(self):
        key = self._fake_profile("work")
        P.set_default(P.load(), "work")
        key.unlink()
        rc, _, err = self._run(["-C", tempfile.mkdtemp(), "status"])
        self.assertEqual(rc, 2)
        self.assertIn("SSH key not found", err)

    def test_unknown_profile_is_a_clean_error(self):
        import contextlib, io
        from gity.cli import main
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            main(["--profile", "ghost", "status"])
        self.assertIn("no such profile: ghost", err.getvalue())
        self.assertNotIn('"', err.getvalue())

    def test_cwd_is_honoured(self):
        from gity.ssh_env import run_git_with_profile
        repo = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            inside = run_git_with_profile(["rev-parse", "--git-dir"], None, cwd=repo, capture=True)
            outside = run_git_with_profile(["rev-parse", "--git-dir"], None,
                                           cwd=tempfile.mkdtemp(), capture=True)
        self.assertEqual(inside, 0)
        self.assertNotEqual(outside, 0)

    def test_dangling_default_and_binding_are_ignored(self):
        cfg = P.load()
        cfg["default_profile"] = "ghost"
        cfg["repos"]["/some/repo"] = "ghost"
        self.assertIsNone(P.resolve_profile(cfg, "/some/repo"))

    def test_invalid_profile_names_rejected(self):
        from gity.cli import cmd_profile
        for bad in ("../evil", "a/b", "-x", ".hidden", "", "a b"):
            with self.assertRaises(ValueError):
                P.add_profile(P.load(), bad, comment="c", email=None, key_bits=2048)
        import contextlib, io
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cmd_profile(["-i", "../evil", "--name", "n", "--email", "e@example.com"])
        self.assertFalse((self.home / "evil_key").exists())
        self.assertEqual(P.load()["profiles"], {})

    def test_import_rejects_path_traversal(self):
        """A crafted export must not be able to write outside the keys dir."""
        outside = Path(tempfile.mkdtemp()) / "pwned"
        for name in ("../escape_key", str(outside), "sub/dir_key", "notakey.txt"):
            payload = {"version": 1, "config": {"profiles": {}}, "files": {name: "ZmFrZQ=="}}
            with self.assertRaises(ValueError):
                P.import_payload(payload)
        self.assertFalse((self.home / "escape_key").exists())
        self.assertFalse(outside.exists())
        bad_profile = {"version": 1, "config": {"profiles": {"../x": {}}}, "files": {}}
        with self.assertRaises(ValueError):
            P.import_payload(bad_profile)

    def test_import_rejects_unknown_version_and_garbage(self):
        with self.assertRaises(ValueError):
            P.import_payload({"version": 99, "config": {}, "files": {}})
        with self.assertRaises(ValueError):
            P.import_payload(["not", "a", "dict"])
        with self.assertRaises(ValueError):
            P.import_payload({"version": 1, "config": {}, "files": {"a_key": "!!not base64!!"}})

    def test_import_of_non_export_file_fails_without_prompting(self):
        from unittest import mock
        from gity.cli import cmd_import
        import contextlib, io
        junk = self.home / "junk.gity"
        junk.write_bytes(b"definitely not an export")
        with mock.patch("getpass.getpass", side_effect=AssertionError("prompted")):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cmd_import(str(junk))

    def test_delete_never_touches_keys_outside_gity_home(self):
        external = Path(tempfile.mkdtemp()) / "id_ed25519"
        external.write_text("my real key")
        cfg = P.load()
        cfg["profiles"]["ext"] = {"key_path": str(external), "public_key_path": str(external)}
        P.save(cfg)
        P.delete_profile(P.load(), "ext")
        self.assertNotIn("ext", P.load()["profiles"])
        self.assertTrue(external.exists())

    def test_scripted_init_never_prompts(self):
        from unittest import mock
        from gity.cli import cmd_profile
        import contextlib, io
        with mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cmd_profile(["-i", "ci", "--name", "CI Bot", "--email", "ci@example.com"])
        self.assertEqual(rc, 0)
        meta = P.load()["profiles"]["ci"]
        self.assertEqual((meta["user_name"], meta["email"], meta["comment"]),
                         ("CI Bot", "ci@example.com", "ci@example.com"))

    def test_interactive_init_survives_closed_stdin(self):
        from gity.cli import cmd_profile
        import contextlib, io
        old = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cmd_profile(["-i", "eof"])
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertIn("eof", P.load()["profiles"])

    def test_corrupt_config_is_kept_aside_not_overwritten(self):
        import contextlib, io
        cfg_file = self.home / "config.json"
        cfg_file.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            cfg = P.load()
        self.assertEqual(cfg["profiles"], {})
        self.assertEqual((self.home / "config.json.corrupt").read_text(), "{not json")

    def test_list_tolerates_sparse_metadata(self):
        from gity.cli import cmd_profile
        import contextlib, io
        cfg = P.load()
        cfg["profiles"]["sparse"] = {"key_type": None, "created_at": None}
        cfg["profiles"]["broken"] = "not a dict"
        P.save(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cmd_profile(["-l"]), 0)
        self.assertIn("sparse", buf.getvalue())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_export_file_is_private_and_encrypted(self):
        from unittest import mock
        from gity import crypto as C
        from gity.cli import cmd_export
        import contextlib, io
        P.add_profile(P.load(), "work", comment="c", email=None, key_bits=2048)
        out = self.home / "backup"
        with mock.patch.object(C, "ask_passphrase", return_value="pw"):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_export(str(out)), 0)
        exported = out.with_suffix(".gity")
        self.assertEqual(os.stat(exported).st_mode & 0o777, 0o600)
        blob = exported.read_bytes()
        self.assertTrue(blob.startswith(b"GITY1"))
        self.assertNotIn(b"OPENSSH PRIVATE KEY", blob)
        self.assertIn("work", json.loads(C.decrypt_bytes("pw", blob))["config"]["profiles"])


if __name__ == "__main__":
    unittest.main()
