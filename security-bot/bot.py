#!/usr/bin/env python3
"""Daily dependency security repairs. Repository code runs only in containers."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import time
import shutil
import tomllib
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

OWNER = "vishalkotak"
RENOVATE_IMAGE = "ghcr.io/renovatebot/renovate:44.234.1-full"
PREFIX = "security-bot/"
SHA = re.compile(r"^[0-9a-f]{40}$")


class Blocked(RuntimeError):
    pass


class PendingCI(Blocked):
    pass


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, method="GET", data=None, with_next=False):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only GitHub API paths are allowed")
        req = Request("https://api.github.com" + path,
                      data=None if data is None else json.dumps(data).encode(),
                      method=method, headers={
                          "Authorization": "Bearer " + self.token,
                          "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28",
                          "Content-Type": "application/json",
                      })
        # Never log request headers, tokens, or secret-scanning matches.
        for attempt in range(3):
            try:
                with urlopen(req, timeout=60) as response:
                    body = response.read()
                    data = json.loads(body) if body else None
                    if not with_next:
                        return data
                    match = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
                    next_path = None
                    if match:
                        target = urlsplit(urljoin("https://api.github.com" + path, match.group(1)))
                        if target.scheme != "https" or target.netloc != "api.github.com":
                            raise Blocked("Refusing a pagination link outside the GitHub API")
                        next_path = target.path + ("?" + target.query if target.query else "")
                    return data, next_path
            except HTTPError as exc:
                if method == "GET" and exc.code in (429, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                message = ""
                try:
                    body = json.loads(exc.read(65536))
                    message = str(body.get("message", ""))[:500]
                except (ValueError, OSError):
                    pass
                detail = f": {message}" if message else ""
                raise Blocked(f"GitHub {method} {path}: HTTP {exc.code}{detail}") from None
            except (URLError, TimeoutError):
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise Blocked(f"GitHub {method} {path}: network error") from None

    def pages(self, path, key=None):
        result = []
        parsed = urlsplit(path)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("per_page", "100")
        path = parsed.path + "?" + urlencode(query)
        visited = set()
        for _ in range(1000):
            if path in visited:
                raise Blocked("Pagination cycle detected; coverage is incomplete")
            visited.add(path)
            # Dependabot uses cursors; other endpoints use numbered pages.
            # Following GitHub's Link header supports both without guessing.
            data, next_path = self.request(path, with_next=True)
            rows = data[key] if key else data
            result.extend(rows)
            if not next_path:
                return result
            path = next_path
        raise Blocked("Pagination limit reached; coverage is incomplete")


def run(args, cwd=None, env=None, timeout=1200, capture=False):
    # Repository commands never receive the host's token or credential files.
    clean_env = {k: v for k, v in os.environ.items()
                 if k in ("PATH", "HOME", "TMPDIR", "LANG", "DOCKER_HOST")}
    if env:
        clean_env.update(env)
    try:
        result = subprocess.run(args, cwd=cwd, env=clean_env, timeout=timeout,
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None,
                                text=True, check=False)
    except subprocess.TimeoutExpired:
        raise Blocked(f"Command timed out after {timeout}s: {args[0]}") from None
    if result.returncode:
        # Do not print exception reprs containing authentication environment.
        raise Blocked(f"Command failed (exit {result.returncode}): {args[0]}")
    return result.stdout.strip() if capture else ""


def git(args, cwd, token=None, capture=False):
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null"}
    if token:
        import base64
        auth = base64.b64encode(("x-access-token:" + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0="AUTHORIZATION: basic " + auth)
    return run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-c", "protocol.file.allow=always", *args], cwd=cwd, env=env, capture=capture)


def safe_sha(value):
    if not SHA.fullmatch(value):
        raise Blocked("Invalid commit SHA")
    return value


def dependency_file(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts:
        return False
    return p.name in {"package.json", "package-lock.json", "npm-shrinkwrap.json",
                      "pyproject.toml", "poetry.lock", "uv.lock", "Cargo.toml", "Cargo.lock"} or bool(
        re.fullmatch(r"(?:requirements|constraints)[^/]*\.(?:txt|in)", p.name))


def eligible(pr, repo, actor):
    return (repo.get("fork") is False
            and pr.get("state") == "open" and not pr.get("draft")
            and pr["user"]["login"] == actor
            and pr["head"]["repo"]["full_name"] == repo["full_name"]
            and pr["base"]["ref"] == repo["default_branch"]
            and pr["head"]["ref"].startswith(PREFIX)
            and "[SECURITY]" in pr["title"].upper()
            and "security-bot" in {label["name"] for label in pr["labels"]})


def check_ci(api, full_name, sha):
    checks = api.pages(f"/repos/{full_name}/commits/{sha}/check-runs?filter=latest", "check_runs")
    statuses = api.pages(f"/repos/{full_name}/commits/{sha}/statuses")
    latest = {}
    for status in statuses:
        latest.setdefault(status["context"], status["state"])
    if any(c["status"] == "completed" and c["conclusion"] != "success" for c in checks):
        raise Blocked("Existing CI has failed, skipped, or neutral checks")
    if any(state in ("failure", "error") for state in latest.values()):
        raise Blocked("Existing commit statuses have failed")
    if any(c["status"] != "completed" for c in checks) or any(state != "success" for state in latest.values()):
        raise PendingCI("Existing CI is still pending")


def wait_for_ci(api, full_name, sha, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check_ci(api, full_name, sha)
        except PendingCI:
            if time.monotonic() >= deadline:
                raise PendingCI("Published commit CI is still pending after the wait budget")
            print(f"Waiting for published commit CI: {full_name}", flush=True)
            time.sleep(10)


def projects(root, changed):
    """Find each changed file's nearest supported project; reject uncovered paths."""
    found = {}
    for name in changed:
        covered = False
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise Blocked(f"Removed or symlinked dependency file: {name}")
        for parent in (path.parent, *path.parent.parents):
            if parent != root and root not in parent.parents:
                break
            if (parent / "package.json").is_file():
                if not (parent / "package-lock.json").is_file():
                    raise Blocked("Node project has no npm lockfile; needs a validator")
                found[parent] = "node"
                covered = True
                break
            if (parent / "Cargo.toml").is_file():
                # Cargo workspace validation starts at the root-most manifest.
                cargo_root = parent
                for ancestor in parent.parents:
                    if ancestor != root and root not in ancestor.parents:
                        break
                    if (ancestor / "Cargo.toml").is_file():
                        cargo_root = ancestor
                found[cargo_root] = "rust"
                covered = True
                break
            if (parent / "pyproject.toml").is_file() or name.endswith((".txt", ".in")):
                found[parent] = "python"
                covered = True
                break
        if not covered:
            raise Blocked(f"No validator covers {name}")
    if not found:
        raise Blocked("No supported projects changed")
    return found


def python_image(project):
    manifest = project / "pyproject.toml"
    if manifest.is_symlink():
        raise Blocked("Symlinked Python manifests are not supported")
    if not manifest.is_file():
        return "python:3.12-bookworm"
    metadata = tomllib.loads(manifest.read_text())
    constraint = metadata.get("project", {}).get("requires-python") or metadata.get(
        "tool", {}).get("poetry", {}).get("dependencies", {}).get("python", "")
    if isinstance(constraint, dict):
        constraint = constraint.get("version", "")
    minimum = re.search(r"(?:\^|>=|==)?\s*3\.(\d+)", str(constraint))
    minor = max(12, int(minimum.group(1))) if minimum else 12
    return f"python:3.{minor}-bookworm"


def container(root, project, kind, timeout=900, prepare=False):
    relative = project.relative_to(root).as_posix()
    images = {"node": "node:22-bookworm", "python": python_image(project), "rust": "rust:1-bookworm"}
    scripts = {
        "node": r'''set -eu
npm ci --ignore-scripts --no-audit
npm audit --audit-level=low
node -e 'const p=require("./package.json"); if (!p.scripts?.test && !p.scripts?.build) process.exit(1)'
if node -e 'process.exit(require("./package.json").scripts?.build ? 0 : 1)'; then npm run build; fi
if node -e 'process.exit(require("./package.json").scripts?.test ? 0 : 1)'; then CI=true npm test; fi
''',
        "python": r'''set -eu
verify_python() {
  if torch_version=$("$@" -c 'from importlib.metadata import version; print(version("torch").split("+")[0])' 2>/dev/null); then
    if [ -n "$torch_version" ]; then
      echo "CPU validation uses the official CPU wheel of locked torch release $torch_version; GPU execution is not validated."
      "$@" -m pip install --force-reinstall --no-deps "torch==$torch_version" --index-url https://download.pytorch.org/whl/cpu
      checked_version=$("$@" -c 'from importlib.metadata import version; print(version("torch").split("+")[0])')
      test "$checked_version" = "$torch_version"
      "$@" -c 'import torch; assert torch.version.cuda is None'
    fi
  fi
  "$@" -m pip check
  if [ -n "${SECURITY_LOCK_REQUIREMENTS:-}" ]; then
    for requirement in "$SECURITY_LOCK_REQUIREMENTS".*.txt; do
      "$@" -m pip_audit -r "$requirement" --no-deps --disable-pip
    done
  else
    "$@" -m pip_audit
  fi
  if "$@" -m pytest; then
    return 0
  else
    test_status=$?
  fi
  if [ "$test_status" -ne 5 ]; then return "$test_status"; fi
  echo 'No unit tests collected: validating dependency compatibility and tracked-source compilation instead.'
  git ls-files -z '*.py' | xargs -0 -r "$@" -m py_compile
}
python -m pip install --disable-pip-version-check pytest pip-audit
if [ -f poetry.lock ]; then
  python -m pip install poetry
  poetry install --no-root --no-interaction
  export PYTHONPATH="$PWD:$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
  poetry run python -m pip install pytest pip-audit
  python /security/poetry_security.py --requirements /tmp/security-locked.txt
  export SECURITY_LOCK_REQUIREMENTS=/tmp/security-locked
  verify_python poetry run python
elif [ -f uv.lock ]; then
  python -m pip install uv
  uv sync --locked --all-extras
  uv pip install --python .venv/bin/python pytest pip-audit
  verify_python .venv/bin/python
else
  if [ -f pyproject.toml ]; then python -m pip install '.[dev]'; fi
  found=false
  for f in requirements*.txt; do
    [ -f "$f" ] || continue
    python -m pip install -r "$f"
    found=true
  done
  if [ ! -f pyproject.toml ] && [ "$found" = false ]; then exit 1; fi
  verify_python python
fi
''',
        "rust": r'''set -eu
test -f Cargo.lock
cargo install cargo-audit --locked
cargo audit
cargo test --workspace --locked
''',
    }
    if prepare:
        if kind == "node":
            scripts[kind] = "set -eu\nnpm audit fix --package-lock-only --ignore-scripts --no-fund\n"
        elif kind == "python":
            scripts[kind] = "set -eu\npython -m pip install poetry pip-audit\npython /security/poetry_security.py\n"
        else:
            raise Blocked("Unsupported preparation phase")
    # Only repository contents are mounted. No host home, SSH agent, Docker
    # socket, GitHub token, or controller directory is visible to project code.
    name = "security-check-" + os.urandom(8).hex()
    print(f"Validating {kind} project {relative} with container UID/GID {os.getuid()}:{os.getgid()}", flush=True)
    probe = '''set -eu
if ! probe=$(mktemp .security-bot-write.XXXXXX); then
  echo 'Validation checkout is not writable by the container user' >&2
  id >&2
  ls -ld . >&2
  exit 1
fi
rm "$probe"
'''
    try:
        run(["docker", "run", "--name", name, "--rm", "--init", "--cap-drop=ALL",
             "--user", f"{os.getuid()}:{os.getgid()}",
             "--security-opt=no-new-privileges", "--pids-limit=512", "--memory=10g",
             "--cpus=4", "--mount", f"type=bind,src={root},dst=/repo",
             "--mount", f"type=bind,src={Path(__file__).with_name('poetry_security.py')},dst=/security/poetry_security.py,readonly",
             "--workdir", "/repo/" + relative, "--env", "CI=true",
             "--env", "HOME=/tmp", "--env", "CARGO_HOME=/tmp/cargo",
             images[kind], "sh", "-c",
             'export PATH="$CARGO_HOME/bin:$HOME/.local/bin:$PATH"\n' + probe + scripts[kind]], timeout=timeout)
    finally:
        # Killing the Docker client alone does not stop a timed-out container.
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)


def prepare_npm(project):
    """Repair transitive findings without exposing the checkout or .git to npm."""
    with tempfile.TemporaryDirectory(prefix="security-npm-") as directory:
        work = Path(directory)
        for name in ("package.json", "package-lock.json"):
            source = project / name
            if source.is_symlink():
                raise Blocked("Symlinked npm manifests are not supported")
            shutil.copyfile(source, work / name)
        original_manifest = (work / "package.json").read_bytes()
        container(work, work, "node", prepare=True)
        output = work / "package-lock.json"
        if output.is_symlink() or (work / "package.json").is_symlink():
            raise Blocked("npm preparation produced symlinked output")
        if (work / "package.json").read_bytes() != original_manifest:
            raise Blocked("npm audit fix unexpectedly changed declared dependency ranges")
        repaired = output.read_text()
        json.loads(repaired)
        destination = project / "package-lock.json"
        if destination.read_text() == repaired:
            return False
        destination.write_text(repaired)
        return True


def prepare_poetry(project):
    if not (project / "poetry.lock").is_file():
        return []
    from poetry_security import allowed_manifest_change
    names = ("pyproject.toml", "poetry.lock")
    with tempfile.TemporaryDirectory(prefix="security-poetry-") as directory:
        work = Path(directory)
        for name in names:
            source = project / name
            if source.is_symlink():
                raise Blocked("Symlinked Poetry manifests are not supported")
            shutil.copyfile(source, work / name)
        before = tomllib.loads((work / "pyproject.toml").read_text())
        container(work, work, "python", prepare=True)
        if any((work / name).is_symlink() for name in names):
            raise Blocked("Poetry preparation produced symlinked manifests")
        after = tomllib.loads((work / "pyproject.toml").read_text())
        if not allowed_manifest_change(before, after):
            raise Blocked("Poetry preparation modified non-dependency configuration")
        tomllib.loads((work / "poetry.lock").read_text())
        changed = []
        for name in names:
            content = (work / name).read_text()
            if (project / name).read_text() != content:
                (project / name).write_text(content)
                changed.append(name)
        return changed


def repair(api, repo, pr, apply):
    if repo.get("fork") is not False:
        raise Blocked("Forked repositories and repositories with unknown fork status are excluded")
    full_name = repo["full_name"]
    number = pr["number"]
    prefix = f"/repos/{full_name}"
    head = safe_sha(pr["head"]["sha"])
    changed_rows = api.pages(f"{prefix}/pulls/{number}/files")
    changed = [f["filename"] for f in changed_rows]
    if not changed or len(changed) > 100 or any(not dependency_file(f) for f in changed):
        raise Blocked("Proposal contains unsupported files or too many changes")
    if any(f["status"] in ("renamed", "removed") for f in changed_rows):
        raise Blocked("Dependency file deletion or rename requires review")
    check_ci(api, full_name, head)
    with tempfile.TemporaryDirectory(prefix="security-bot-") as temp:
        temp = Path(temp)
        stage = temp / "stage.git"
        target = temp / "target"
        git(["init", "--bare", str(stage)], temp)
        git(["remote", "add", "origin", f"https://github.com/{full_name}.git"], stage)
        # Fetch GitHub-generated refs, never a PR-supplied remote URL.
        git(["fetch", "--no-tags", "origin",
             f"refs/heads/{repo['default_branch']}:refs/heads/base",
             f"refs/pull/{number}/head:refs/heads/candidate"], stage, token=api.token)
        base = safe_sha(git(["rev-parse", "base"], stage, capture=True))
        if git(["rev-parse", "candidate"], stage, capture=True) != head:
            raise Blocked("Proposal changed while fetching")
        git(["clone", "--no-hardlinks", "--no-checkout", str(stage), str(target)], temp)
        git(["checkout", "--detach", base], target)
        git(["-c", "user.name=Security Repair Bot", "-c",
             f"user.email={OWNER}@users.noreply.github.com", "merge", "--no-ff",
             "--no-edit", "-m", f"Merge security repair #{number}", head], target)
        selected = [(p.relative_to(target).as_posix(), kind)
                    for p, kind in projects(target, changed).items()]
        added_locks = []
        for relative, kind in selected:
            if kind == "node" and prepare_npm(target / relative):
                added_locks.append((Path(relative) / "package-lock.json").as_posix())
            if kind == "python":
                added_locks.extend((Path(relative) / name).as_posix() for name in prepare_poetry(target / relative))
        if added_locks:
            git(["add", "--", *added_locks], target)
            git(["-c", "user.name=Security Repair Bot", "-c",
                 f"user.email={OWNER}@users.noreply.github.com", "commit", "-m",
                 "Fix remaining dependency vulnerabilities"], target)
        validated = safe_sha(git(["rev-parse", "HEAD"], target, capture=True))
        # Save the exact commit in a separate bare repo BEFORE running any
        # project code. Project code cannot alter the objects later published.
        git(["fetch", str(target), "HEAD:refs/heads/validated"], stage)
        deadline = time.monotonic() + 900
        for relative, kind in selected:
            container(target, target / relative, kind, max(1, int(deadline - time.monotonic())))
        if not apply:
            return {"status": "validated-dry-run", "commit": validated}
        current = api.request(f"{prefix}/pulls/{number}")
        if current["state"] != "open" or current["head"]["sha"] != head:
            raise Blocked("Proposal changed after validation")
        check_ci(api, full_name, head)
        # A normal fast-forward push rejects a concurrent default-branch edit.
        # No force, ruleset bypass, or branch-protection changes are used.
        git(["push", "origin", f"{validated}:refs/heads/{repo['default_branch']}"],
            stage, token=api.token)
        result = {"status": "published", "commit": validated, "post_validation": "pending"}
        try:
            # Use a fresh checkout of the published object, not files left by tests.
            git(["fetch", "--no-tags", "origin",
                 f"refs/heads/{repo['default_branch']}:refs/heads/published"],
                stage, token=api.token)
            git(["merge-base", "--is-ancestor", validated, "published"], stage)
            post = temp / "post"
            git(["clone", "--no-hardlinks", "--no-checkout", str(stage), str(post)], temp)
            git(["checkout", "--detach", validated], post)
            deadline = time.monotonic() + 900
            for relative, kind in selected:
                container(post, post / relative, kind, max(1, int(deadline - time.monotonic())))
            result["post_validation"] = "passed"
            try:
                wait_for_ci(api, full_name, validated)
                result["published_commit_ci"] = "all currently reported checks passed"
            except Blocked as exc:
                result["published_commit_ci"] = str(exc)
                result["post_validation"] = "FAILED: published commit CI is pending or unsuccessful"
        except Blocked as exc:
            result["post_validation"] = "FAILED: " + str(exc)
            # Do not silently revert a branch that could have subsequent edits.
        return result


def renovate(repositories, token, dry_run, directory):
    # The official image runs as a non-root user.
    directory.chmod(0o755)
    config = {
        "platform": "github", "repositories": repositories,
        "onboarding": False, "requireConfig": "ignored", "forkProcessing": "disabled",
        "extends": ["security:only-security-updates"],
        "enabledManagers": ["npm", "pip_requirements", "pip_setup", "poetry", "pep621", "cargo"],
        "osvVulnerabilityAlerts": True, "branchPrefix": PREFIX,
        "labels": ["security-bot"], "automerge": False,
        "separateMajorMinor": False,
        "allowScripts": False, "allowPlugins": False, "allowedCommands": [],
        "ignoreScripts": True, "dependencyDashboard": False,
        "vulnerabilityAlerts": {"enabled": True, "groupName": "security fixes",
                                "commitMessageSuffix": "[SECURITY]"},
    }
    if dry_run:
        config["dryRun"] = "full"
    config_path = directory / "renovate.json"
    config_path.write_text(json.dumps(config))
    run(["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges",
         "--env", "RENOVATE_TOKEN", "--env", "RENOVATE_CONFIG_FILE=/config/renovate.json",
         "--mount", f"type=bind,src={directory},dst=/config,readonly", RENOVATE_IMAGE],
        env={"RENOVATE_TOKEN": token}, timeout=5400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Create proposals and publish validated repairs")
    parser.add_argument("--skip-renovate", action="store_true")
    parser.add_argument("--report", default="security-report.json")
    args = parser.parse_args()
    token = os.environ.get("SECURITY_BOT_TOKEN")
    if not token:
        raise SystemExit("Activation required: add SECURITY_BOT_TOKEN to this repository's Actions secrets")
    api = GitHub(token)
    actor = api.request("/user")["login"]
    repos = [r for r in api.pages("/user/repos?affiliation=owner&sort=full_name")
             if r["owner"]["login"].lower() == OWNER.lower()]
    report = {"owner": OWNER, "apply": args.apply, "repositories": [], "errors": [],
              "torch_validation": "Official CPU wheel of locked release; GPU execution is not validated",
              "python_no_test_policy": "Audit, pip check, and tracked-source compilation; no unit tests claimed"}
    failed = False
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="security-renovate-") as temp:
            active = [r["full_name"] for r in repos if r.get("fork") is False
                      and not r["archived"] and not r["disabled"]]
            if not args.skip_renovate and active:
                try:
                    renovate(active, token, not args.apply, Path(temp))
                except Blocked as exc:
                    report["errors"].append(str(exc))
                    failed = True
        for repo in repos:
            name = repo["full_name"]
            row = {"repository": name, "repairs": []}
            report["repositories"].append(row)
            if repo.get("fork") is not False:
                row["status"] = "skipped: fork or unknown fork status"
                continue
            if repo["archived"] or repo["disabled"]:
                row["status"] = "read-only: archived or disabled"
                continue
            try:
                # These are real alerts, not email-subject guesses. A permission
                # error is reported as a coverage gap, never as a clean scan.
                alerts = api.pages(f"/repos/{name}/dependabot/alerts?state=open")
                row["dependency_alerts"] = [{"number": a["number"], "url": a["html_url"],
                    "advisory": a["security_advisory"]["ghsa_id"],
                    "severity": a["security_advisory"]["severity"]} for a in alerts]
                prs = api.pages(f"/repos/{name}/pulls?state=open")
                candidates = [p for p in prs if eligible(p, repo, actor)]
                # Bound daily work and prevent competing repairs of one branch.
                for pr in candidates[:1]:
                    item = {"pr": pr["html_url"], "status": "started"}
                    row["repairs"].append(item)
                    try:
                        if time.monotonic() - started > 4 * 3600:
                            raise Blocked("Daily runtime budget reached; retry next run")
                        item.update(repair(api, repo, pr, args.apply))
                        if item.get("post_validation", "").startswith("FAILED"):
                            failed = True
                    except (Blocked, OSError) as exc:
                        item.update(status="blocked", reason=str(exc))
                        failed = True
                row["remaining_proposals"] = max(0, len(candidates) - 1)
                row["status"] = "scanned"
            except Blocked as exc:
                row.update(status="coverage-gap", reason=str(exc))
                failed = True
    finally:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
        # Keep the actual failure reasons in the job log as well as the artifact.
        # No credentials, source files, or secret-scanning matches are included.
        for error in report["errors"]:
            print("Controller error: " + error, flush=True)
        for row in report["repositories"]:
            if row.get("reason"):
                print(f"{row['repository']}: {row['reason']}", flush=True)
            for item in row["repairs"]:
                if item.get("reason") or item.get("post_validation", "").startswith("FAILED"):
                    print(f"{row['repository']}: {item.get('reason', item.get('post_validation'))}", flush=True)
        if failed:
            print(f"Security run incomplete; details saved in {args.report}", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as f:
                f.write("## Daily security repairs\n\n")
                for row in report["repositories"]:
                    count = len(row['dependency_alerts']) if 'dependency_alerts' in row else 'unknown'
                    f.write(f"- **{row['repository']}**: {row.get('status', 'incomplete')}; "
                            f"{count} open dependency alerts\n")
                    for item in row["repairs"]:
                        f.write(f"  - {item['pr']}: {item['status']} "
                                f"{item.get('reason', item.get('post_validation', ''))}\n")
                f.write("\nSee security-report.json for details. Source-code and secret "
                        "scanning are not implemented by this dependency repair controller.\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
