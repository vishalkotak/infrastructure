import json
import os
from pathlib import Path
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch

import bot


class Response:
    def __init__(self, data, link=""):
        self.data = data
        self.headers = {"Link": link}

    def read(self):
        return json.dumps(self.data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Pagination(unittest.TestCase):
    def test_dependabot_uses_server_cursor_and_never_invents_page_parameter(self):
        url = "https://api.github.com/repos/x/y/dependabot/alerts?state=open&per_page=100&after=cursor123"
        replies = [Response([{"number": 1}], f'<{url}>; rel="next"'), Response([{"number": 2}])]
        with patch("bot.urlopen", side_effect=replies) as request:
            rows = bot.GitHub("test-only").pages("/repos/x/y/dependabot/alerts?state=open")
        self.assertEqual([r["number"] for r in rows], [1, 2])
        first_url = request.call_args_list[0].args[0].full_url
        self.assertNotIn("page=", first_url.replace("per_page=", ""))
        self.assertEqual(request.call_args_list[1].args[0].full_url, url)

    def test_numbered_endpoints_follow_link_header_too(self):
        replies = [Response({"check_runs": [1]}, '<https://api.github.com/checks?page=2>; rel="next"'),
                   Response({"check_runs": [2]})]
        with patch("bot.urlopen", side_effect=replies):
            self.assertEqual(bot.GitHub("test-only").pages("/checks", "check_runs"), [1, 2])

    def test_full_page_without_next_link_does_not_make_another_request(self):
        with patch("bot.urlopen", return_value=Response(list(range(100)))) as request:
            self.assertEqual(len(bot.GitHub("test-only").pages("/alerts")), 100)
        self.assertEqual(request.call_count, 1)

    def test_pagination_cannot_send_credentials_to_another_host(self):
        with patch("bot.urlopen", return_value=Response([], '<https://evil.example/steal>; rel="next"')) as request:
            with self.assertRaises(bot.Blocked):
                bot.GitHub("test-only").pages("/alerts")
        self.assertEqual(request.call_count, 1)

    def test_repeated_link_fails_instead_of_truncating_coverage(self):
        link = '<https://api.github.com/alerts?per_page=100>; rel="next"'
        with patch("bot.urlopen", return_value=Response([1], link)) as request:
            with self.assertRaises(bot.Blocked):
                bot.GitHub("test-only").pages("/alerts")
        self.assertEqual(request.call_count, 1)


class API:
    def __init__(self, checks=(), statuses=()):
        self.checks, self.statuses = list(checks), list(statuses)

    def pages(self, path, key=None):
        return self.checks if key else self.statuses


class Gates(unittest.TestCase):
    def test_validation_containers_use_runner_uid_and_disposable_home(self):
        with patch("bot.run") as run, patch("bot.subprocess.run"):
            bot.container(Path("/tmp/repo"), Path("/tmp/repo/project"), "python")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertIn("HOME=/tmp", command)
        self.assertIn("CARGO_HOME=/tmp/cargo", command)
        self.assertNotIn(os.environ.get("SECURITY_BOT_TOKEN", "impossible-token-value"), " ".join(command))

    def test_forks_are_excluded_from_scans_and_renovate(self):
        repos = [{"full_name": f"{bot.OWNER}/{name}", "owner": {"login": bot.OWNER},
                  "archived": False, "disabled": False, **metadata}
                 for name, metadata in [("original", {"fork": False}), ("forked", {"fork": True}),
                                        ("unknown", {})]]
        calls = []

        class FakeAPI:
            def __init__(self, token):
                pass

            def request(self, path):
                return {"login": bot.OWNER}

            def pages(self, path):
                calls.append(path)
                return repos if path.startswith("/user/repos") else []

        with tempfile.TemporaryDirectory() as directory:
            report_path = str(Path(directory) / "report.json")
            with patch("bot.GitHub", FakeAPI), patch("bot.renovate") as renovate, \
                    patch.dict(os.environ, {"SECURITY_BOT_TOKEN": "test-only"}), \
                    patch("sys.argv", ["bot.py", "--report", report_path]):
                self.assertEqual(bot.main(), 0)
            self.assertEqual(renovate.call_args.args[0], [f"{bot.OWNER}/original"])
            self.assertFalse(any("forked" in path or "unknown" in path for path in calls))
            report = json.loads(Path(report_path).read_text())
            self.assertTrue(report["repositories"][1]["status"].startswith("skipped"))

    def test_publisher_rejects_forks_before_any_api_or_git_work(self):
        for metadata in [{"fork": True}, {}]:
            with self.assertRaises(bot.Blocked):
                bot.repair(None, metadata, {}, True)

    def test_reported_api_failure_is_visible_in_job_log(self):
        repo = {"full_name": "x/y", "owner": {"login": bot.OWNER},
                "archived": False, "disabled": False, "fork": False}

        class FakeAPI:
            def __init__(self, token):
                pass

            def request(self, path):
                return {"login": bot.OWNER}

            def pages(self, path):
                if path.startswith("/user/repos"):
                    return [repo]
                raise bot.Blocked("GitHub alert API: HTTP 403: missing permission")

        with tempfile.TemporaryDirectory() as directory:
            report_path = str(Path(directory) / "report.json")
            output = io.StringIO()
            with patch("bot.GitHub", FakeAPI), \
                    patch.dict(os.environ, {"SECURITY_BOT_TOKEN": "test-only"}), \
                    patch("sys.argv", ["bot.py", "--skip-renovate", "--report", report_path]), \
                    redirect_stdout(output):
                result = bot.main()
            self.assertEqual(result, 1)
            self.assertIn("HTTP 403: missing permission", output.getvalue())
            self.assertNotIn("test-only", output.getvalue())
            self.assertEqual(json.loads(Path(report_path).read_text())["repositories"][0]["status"], "coverage-gap")

    def test_failed_or_incomplete_ci_blocks(self):
        for status, conclusion in [("in_progress", None), ("completed", "failure"),
                                   ("completed", "skipped"), ("completed", "neutral")]:
            with self.subTest(status=status, conclusion=conclusion):
                with self.assertRaises(bot.Blocked):
                    bot.check_ci(API([{"status": status, "conclusion": conclusion}]), "x/y", "a")

    def test_latest_status_wins_but_other_contexts_still_block(self):
        bot.check_ci(API(statuses=[{"context": "test", "state": "success"},
                                  {"context": "test", "state": "failure"}]), "x/y", "a")
        with self.assertRaises(bot.Blocked):
            bot.check_ci(API(statuses=[{"context": "test", "state": "success"},
                                      {"context": "build", "state": "pending"}]), "x/y", "a")

    def test_changes_to_executables_workflows_and_traversal_block(self):
        for path in ["setup.py", "app.py", ".github/workflows/security.yml",
                     "../package.json", "/package.json", "requirements.txt.sh"]:
            self.assertFalse(bot.dependency_file(path), path)
        for path in ["frontend/package-lock.json", "backend/requirements-dev.txt",
                     "Cargo.lock", "pyproject.toml"]:
            self.assertTrue(bot.dependency_file(path), path)

    def test_unsupported_project_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").mkdir()
            (root / "a/package.json").write_text("{}")
            (root / "a/package-lock.json").write_text("{}")
            (root / "b").mkdir()
            (root / "b/uv.lock").write_text("")
            with self.assertRaises(bot.Blocked):
                bot.projects(root, ["a/package.json", "b/uv.lock"])

    def test_symlinked_manifest_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").symlink_to("/etc/hosts")
            with self.assertRaises(bot.Blocked):
                bot.projects(root, ["requirements.txt"])

    def test_rust_workspace_validates_at_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.toml").touch()
            (root / "member").mkdir()
            (root / "member/Cargo.toml").touch()
            self.assertEqual(bot.projects(root, ["member/Cargo.toml"]), {root: "rust"})

    def test_only_own_security_proposals_are_eligible(self):
        repo = {"full_name": "x/y", "default_branch": "main", "fork": False}
        pr = {"state": "open", "draft": False, "user": {"login": "actor"},
              "head": {"repo": {"full_name": "x/y"}, "ref": "security-bot/foo"},
              "base": {"ref": "main"}, "title": "Update foo [SECURITY]",
              "labels": [{"name": "security-bot"}]}
        self.assertTrue(bot.eligible(pr, repo, "actor"))
        self.assertTrue(bot.eligible({**pr, "title": "chore(deps): update foo [security]"}, repo, "actor"))
        for change in [{"user": {"login": "outsider"}}, {"draft": True},
                       {"title": "Regular upgrade"}, {"base": {"ref": "other"}},
                       {"head": {"repo": {"full_name": "evil/fork"}, "ref": "security-bot/foo"}}]:
            self.assertFalse(bot.eligible({**pr, **change}, repo, "actor"))

    def test_repo_code_never_receives_host_token(self):
        with patch.dict(os.environ, {"SECURITY_BOT_TOKEN": "private", "GH_TOKEN": "private"}):
            with patch("subprocess.run") as run:
                run.return_value.returncode = 0
                bot.run(["true"])
                env = run.call_args.kwargs["env"]
                self.assertNotIn("SECURITY_BOT_TOKEN", env)
                self.assertNotIn("GH_TOKEN", env)

    def test_no_force_push_even_when_default_branch_moves(self):
        # Exercise actual Git's publication rule on local disposable repos.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, a, b = [root / n for n in ("remote.git", "a", "b")]
            bot.git(["init", "--bare", str(remote)], root)
            bot.git(["clone", str(remote), str(a)], root)
            for repo in [a]:
                bot.git(["config", "user.name", "Test"], repo)
                bot.git(["config", "user.email", "test@example.com"], repo)
            (a / "file").write_text("base")
            bot.git(["add", "."], a)
            bot.git(["commit", "-m", "base"], a)
            bot.git(["push", "origin", "HEAD:refs/heads/main"], a)
            bot.git(["clone", "--branch", "main", str(remote), str(b)], root)
            bot.git(["config", "user.name", "Test"], b)
            bot.git(["config", "user.email", "test@example.com"], b)
            (a / "file").write_text("tested candidate")
            bot.git(["commit", "-am", "candidate"], a)
            (b / "other").write_text("concurrent edit")
            bot.git(["add", "."], b)
            bot.git(["commit", "-m", "concurrent"], b)
            bot.git(["push", "origin", "HEAD:refs/heads/main"], b)
            with self.assertRaises(bot.Blocked):
                bot.git(["push", "origin", "HEAD:refs/heads/main"], a)

    def test_publishes_saved_commit_and_post_validates_fresh_checkout(self):
        real_git = bot.git
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, seed = root / "remote.git", root / "seed"
            real_git(["init", "--bare", str(remote)], root)
            real_git(["clone", str(remote), str(seed)], root)
            real_git(["config", "user.name", "Test"], seed)
            real_git(["config", "user.email", "test@example.com"], seed)
            (seed / "package.json").write_text('{"scripts":{"build":"true"}}')
            (seed / "package-lock.json").write_text("{}")
            real_git(["add", "."], seed)
            real_git(["commit", "-m", "base"], seed)
            real_git(["push", "origin", "HEAD:refs/heads/main"], seed)
            base = real_git(["rev-parse", "HEAD"], seed, capture=True)
            (seed / "package-lock.json").write_text('{"fixed":true}')
            real_git(["commit", "-am", "fix"], seed)
            head = real_git(["rev-parse", "HEAD"], seed, capture=True)
            real_git(["push", "origin", "HEAD:refs/pull/1/head"], seed)
            pr = {"number": 1, "head": {"sha": head}, "state": "open"}
            repo = {"full_name": "x/y", "default_branch": "main", "fork": False}

            class FakeAPI:
                token = "test-token-never-used"

                def pages(self, path, key=None):
                    if path.endswith("/files"):
                        return [{"filename": "package-lock.json", "status": "modified"}]
                    return []

                def request(self, path):
                    return pr

            def local_git(args, cwd, token=None, capture=False):
                if args[:3] == ["remote", "add", "origin"]:
                    args = [*args[:3], str(remote)]
                return real_git(args, cwd, capture=capture)

            calls = []

            def fake_test(target, project, kind, timeout=900):
                calls.append(target.name)
                self.assertEqual(json.loads((target / "package-lock.json").read_text()), {"fixed": True})
                if target.name == "target":
                    # Simulate repository code modifying its own checkout and
                    # committing a different result during the first test run.
                    (target / "package-lock.json").write_text('{"tampered":true}')
                    real_git(["-c", "user.name=Test", "-c", "user.email=test@example.com",
                              "commit", "-am", "tampered"], target)

            with patch("bot.git", side_effect=local_git), patch("bot.container", side_effect=fake_test):
                result = bot.repair(FakeAPI(), repo, pr, True)
            self.assertEqual(result["post_validation"], "passed")
            self.assertEqual(calls, ["target", "post"])
            published = real_git(["rev-parse", "refs/heads/main"], remote, capture=True)
            self.assertEqual(result["commit"], published)
            contents = real_git(["show", published + ":package-lock.json"], remote, capture=True)
            self.assertEqual(json.loads(contents), {"fixed": True})
            self.assertNotEqual(base, published)


if __name__ == "__main__":
    unittest.main()
