#!/usr/bin/env python3
"""Install the prepared controller using the user's GitHub CLI authentication."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import re

REPO = "vishalkotak/infrastructure"
FILES = {"bot.py": "security-bot/bot.py", "test_bot.py": "security-bot/test_bot.py",
         "README.md": "security-bot/README.md",
         "install.py": "security-bot/install.py", "test_install.py": "security-bot/test_install.py",
         "daily-security.yml": ".github/workflows/daily-security.yml"}


def gh(*args, payload=None):
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--input", "-"])
    method = args[args.index("--method") + 1] if "--method" in args else "GET"
    read_only = method == "GET" and payload is None
    for attempt in range(3 if read_only else 1):
        result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                                text=True, capture_output=True)
        if not result.returncode:
            try:
                if not result.stdout.strip() and read_only:
                    raise ValueError("Empty JSON response")
                return json.loads(result.stdout) if result.stdout.strip() else None
            except ValueError:
                transient = True
                reason = "GitHub returned an empty or incomplete JSON response."
        else:
            transient = any(term in result.stderr.lower() for term in (
                "unexpected end of json", "http 429", "http 500", "http 502", "http 503", "http 504",
                "error connecting", "connection reset", "timeout"))
            reason = result.stderr
        if read_only and transient and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        raise SystemExit("GitHub CLI request failed:\n" + reason)


def ensure_workflow_scope():
    result = subprocess.run(["gh", "api", "--include", "user"],
                            text=True, capture_output=True)
    if result.returncode:
        raise SystemExit("Cannot reach GitHub through this terminal. "
                         "Run the installer from the terminal where gh auth login succeeded.\n"
                         + result.stderr)
    scope_header = next((line for line in result.stdout.splitlines()
                         if line.lower().startswith("x-oauth-scopes:")), None)
    # Fine-grained tokens have no OAuth scope header. Their permissions are
    # enforced by GitHub when the installer writes repository/workflow files.
    if scope_header:
        scopes = {scope.strip() for scope in scope_header.split(":", 1)[1].split(",")}
        # GitHub normalizes security_events into its broader repo scope.
        # Actual Dependabot access is checked separately below.
        if not {"repo", "workflow"}.issubset(scopes):
            print("GitHub authorization needs repository, workflow, and security-alert access.", flush=True)
            result = subprocess.run(["gh", "auth", "refresh", "--hostname", "github.com",
                                     "--scopes", "repo,workflow,security_events"], check=False)
            if result.returncode:
                raise SystemExit("Workflow authorization was not completed; nothing has been uploaded.")


class AlertAccessError(RuntimeError):
    pass


def verify_controller_client(token, repos):
    # Exercise the actual production client and its pagination against live
    # endpoints, rather than relying only on the CLI's separate HTTP client.
    from bot import GitHub, Blocked
    client = GitHub(token)
    try:
        for repo in repos:
            client.pages(f"/repos/{repo['full_name']}/dependabot/alerts?state=open")
    except Blocked as exc:
        raise AlertAccessError("Production API client preflight failed: " + str(exc)) from None


def credential_api(token, path, method="GET", paginate=False):
    command = ["gh", "api", "--method", method, path]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    result = subprocess.run(command, env={**os.environ, "GH_TOKEN": token},
                            text=True, capture_output=True)
    try:
        body = json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        body = None
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", result.stderr)
        code = status.group(1) if status else "unknown"
        message = body.get("message", "Request failed") if isinstance(body, dict) else "Request failed"
        # No stderr dump: credential values never belong in diagnostics.
        raise AlertAccessError(f"{method} {path}: HTTP {code}: {message}")
    return body


def wait_for_alerts(token, path, attempts=12):
    for attempt in range(attempts):
        try:
            return credential_api(token, path)
        except AlertAccessError as exc:
            reason = str(exc).lower()
            delayed = "http 403" in reason and any(term in reason for term in ("disabled", "not enabled"))
            if not delayed or attempt == attempts - 1:
                raise
            print(f"Waiting for GitHub to initialize alerts ({attempt + 1}/{attempts}): {path.split('/dependabot')[0]}",
                  flush=True)
            time.sleep(5)


def verify_alert_access(token):
    """Test the scheduled credential itself, not merely the installer's login."""
    actor = credential_api(token, "user")
    if actor["login"].lower() != "vishalkotak":
        raise AlertAccessError("The scheduled credential must belong to vishalkotak")
    pages = credential_api(token, "user/repos?affiliation=owner&per_page=100", paginate=True)
    repos = [repo for page in pages for repo in page
             if repo["owner"]["login"].lower() == "vishalkotak"
             and repo.get("fork") is False
             and not repo.get("archived") and not repo.get("disabled")]
    if not repos:
        raise AlertAccessError("The scheduled credential cannot discover any active owned non-fork repositories")
    failures = []
    for repo in repos:
        path = f"repos/{repo['full_name']}/dependabot/alerts?per_page=1"
        try:
            credential_api(token, path)
        except AlertAccessError as exc:
            reason = str(exc)
            # GitHub also returns 403 when alerts are disabled. Enable only
            # that explicitly reported missing feature; never bypass access.
            if "HTTP 403" in reason and any(term in reason.lower() for term in ("disabled", "not enabled")):
                try:
                    credential_api(token, f"repos/{repo['full_name']}/vulnerability-alerts", method="PUT")
                    wait_for_alerts(token, path)
                    print(f"Enabled and verified Dependabot alerts: {repo['full_name']}", flush=True)
                except AlertAccessError as enable_error:
                    failures.append(str(enable_error))
            else:
                failures.append(reason)
    if failures:
        raise AlertAccessError("Scheduled credential cannot read all repository alerts:\n" + "\n".join(failures))
    verify_controller_client(token, repos)
    print(f"Verified scheduled credential can read Dependabot alerts in {len(repos)} repositories.", flush=True)


def save_scheduled_token(prompt=False):
    if prompt:
        token = getpass.getpass("SECURITY_BOT_TOKEN (hidden): ")
    else:
        result = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                                text=True, capture_output=True)
        if result.returncode:
            raise SystemExit("Could not retrieve the authenticated GitHub credential; no secret was saved.")
        token = result.stdout.strip()
    if not token:
        raise SystemExit("Empty credential; no secret was saved.")
    try:
        verify_alert_access(token)
    except AlertAccessError as exc:
        token = ""
        raise SystemExit(f"No new secret was saved and no cloud run was started.\n{exc}") from None
    # A pipe keeps the token out of argv, console output, and tracked files.
    result = subprocess.run(["gh", "secret", "set", "SECURITY_BOT_TOKEN", "--repo", REPO],
                            input=token, text=True, capture_output=True)
    token = ""
    if result.returncode:
        raise SystemExit("Controller uploaded, but secret setup failed. Add SECURITY_BOT_TOKEN in repository settings.")
    print("Scheduled credential saved directly to GitHub Actions.")


def dispatch_run(branch, apply=False, expected_sha=None):
    endpoint = f"repos/{REPO}/actions/workflows/daily-security.yml"
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps({"ref": branch, "inputs": {"apply": apply}, "return_run_details": True})

    def accept_run(run):
        if expected_sha and run.get("head_sha") != expected_sha:
            # Cancel this specific dispatch, not an unrelated concurrent run.
            gh("api", "--method", "POST", f"repos/{REPO}/actions/runs/{run['id']}/cancel")
            print(f"Cancelled stale run {run['id']}: expected revision {expected_sha[:12]}, "
                  f"got {run.get('head_sha', 'unknown')[:12]}. Retrying after indexing.", flush=True)
            time.sleep(5)
            return False
        print(f"Cloud run: {run['html_url']}", flush=True)
        if expected_sha:
            print(f"Verified cloud run uses deployed revision {expected_sha}.", flush=True)
        return True

    def latest_new_run():
        data = gh("api", f"{endpoint}/runs?event=workflow_dispatch&per_page=10")
        runs = [run for run in data["workflow_runs"] if run["created_at"] >= since]
        return runs[0] if runs else None

    for attempt in range(5):
        response = subprocess.run(["gh", "api", "--method", "POST", endpoint + "/dispatches",
                                   "--input", "-"], input=payload, text=True, capture_output=True)
        if response.returncode == 0:
            mode = "live" if apply else "dry-run"
            print(f"GitHub accepted the cloud {mode} request.", flush=True)
            try:
                details = json.loads(response.stdout)
            except ValueError:
                details = {}
            if details.get("workflow_run_id"):
                run = gh("api", f"repos/{REPO}/actions/runs/{details['workflow_run_id']}")
                if accept_run(run):
                    return run
                continue
            for _ in range(10):
                run = latest_new_run()
                if run:
                    if accept_run(run):
                        return run
                    break
                time.sleep(3)
            else:
                raise SystemExit("Dispatch accepted, but the run could not be verified. "
                                 f"Check https://github.com/{REPO}/actions before retrying.")
            continue
        retryable = any(f"HTTP {status}" in response.stderr for status in (404, 429, 500, 502, 503, 504))
        if not retryable:
            raise SystemExit("Controller and secret are installed, but dispatch failed:\n" + response.stderr)
        print(f"GitHub dispatch is temporarily unavailable (attempt {attempt + 1}/5).", flush=True)
        time.sleep(2 ** (attempt + 1))
        # A failed response can still correspond to an accepted request.
        try:
            run = latest_new_run()
        except SystemExit:
            run = None
        if run:
            if accept_run(run):
                return run
    raise SystemExit("Controller and secret remain installed. GitHub dispatch still fails; "
                     "retry later with: python3 install.py --resume")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-token", action="store_true",
                        help="Use a separate fine-grained token entered through hidden input")
    parser.add_argument("--skip-secret", action="store_true",
                        help="Keep an existing SECURITY_BOT_TOKEN Actions secret")
    parser.add_argument("--resume", action="store_true",
                        help="Retry cloud validation without reinstalling files or changing the secret")
    parser.add_argument("--apply", action="store_true",
                        help="Start a live cloud run that publishes validated fixes without approvals")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    if args.resume:
        repo = gh("api", f"repos/{REPO}")
        ref = gh("api", f"repos/{REPO}/git/ref/heads/{repo['default_branch']}")
        dispatch_run(repo["default_branch"], apply=args.apply, expected_sha=ref["object"]["sha"])
        return
    tests = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=here,
                           text=True, capture_output=True)
    if tests.returncode:
        raise SystemExit("Controller tests failed; no files were uploaded.\n" + tests.stdout + tests.stderr)
    print("Controller safeguard tests passed (disposable local repositories only).", flush=True)
    ensure_workflow_scope()
    user = gh("api", "user")["login"]
    if user.lower() != "vishalkotak":
        raise SystemExit(f"Expected vishalkotak authentication, got {user}")
    repo = gh("api", f"repos/{REPO}")
    branch = repo["default_branch"]
    ref = gh("api", f"repos/{REPO}/git/ref/heads/{branch}")
    parent = ref["object"]["sha"]
    installed_sha = parent
    commit = gh("api", f"repos/{REPO}/git/commits/{parent}")
    tree = gh("api", "--method", "POST", f"repos/{REPO}/git/trees", payload={
        "base_tree": commit["tree"]["sha"],
        "tree": [{"path": target, "mode": "100644", "type": "blob",
                  "content": (here / source).read_text()} for source, target in FILES.items()],
    })
    if tree["sha"] == commit["tree"]["sha"]:
        print("The latest controller is already installed.")
    else:
        created = gh("api", "--method", "POST", f"repos/{REPO}/git/commits", payload={
            "message": "Add daily validated dependency security repairs for all owned repos",
            "tree": tree["sha"], "parents": [parent],
        })
        gh("api", "--method", "PATCH", f"repos/{REPO}/git/refs/heads/{branch}",
           payload={"sha": created["sha"], "force": False})
        installed_sha = created["sha"]
        print(f"Installed: https://github.com/{REPO}/commit/{created['sha']}")
    if not args.skip_secret:
        save_scheduled_token(args.prompt_token)
    dispatch_run(branch, apply=args.apply, expected_sha=installed_sha)
    print("Review that run. Scheduled runs apply passing fixes daily at 08:00 Pacific.")


if __name__ == "__main__":
    main()
