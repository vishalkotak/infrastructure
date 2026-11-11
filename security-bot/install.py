#!/usr/bin/env python3
"""Install the prepared controller using the user's GitHub CLI authentication."""
import argparse
import getpass
import json
from pathlib import Path
import subprocess
import sys

REPO = "vishalkotak/infrastructure"
FILES = {"bot.py": "security-bot/bot.py", "test_bot.py": "security-bot/test_bot.py",
         "README.md": "security-bot/README.md",
         "install.py": "security-bot/install.py", "test_install.py": "security-bot/test_install.py",
         "daily-security.yml": ".github/workflows/daily-security.yml"}


def gh(*args, payload=None):
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(command, input=None if payload is None else json.dumps(payload),
                            text=True, capture_output=True)
    if result.returncode:
        raise SystemExit("GitHub CLI request failed. Check authentication and repository/workflow permissions.\n"
                         + result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None


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
        if not {"repo", "workflow"}.issubset(scopes):
            print("GitHub needs the workflow scope to install the scheduled workflow.", flush=True)
            result = subprocess.run(["gh", "auth", "refresh", "--hostname", "github.com",
                                     "--scopes", "repo,workflow"], check=False)
            if result.returncode:
                raise SystemExit("Workflow authorization was not completed; nothing has been uploaded.")


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
    # A pipe keeps the token out of argv, console output, and tracked files.
    result = subprocess.run(["gh", "secret", "set", "SECURITY_BOT_TOKEN", "--repo", REPO],
                            input=token, text=True, capture_output=True)
    token = ""
    if result.returncode:
        raise SystemExit("Controller uploaded, but secret setup failed. Add SECURITY_BOT_TOKEN in repository settings.")
    print("Scheduled credential saved directly to GitHub Actions.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-token", action="store_true",
                        help="Use a separate fine-grained token entered through hidden input")
    parser.add_argument("--skip-secret", action="store_true",
                        help="Keep an existing SECURITY_BOT_TOKEN Actions secret")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=here, check=True)
    ensure_workflow_scope()
    user = gh("api", "user")["login"]
    if user.lower() != "vishalkotak":
        raise SystemExit(f"Expected vishalkotak authentication, got {user}")
    repo = gh("api", f"repos/{REPO}")
    branch = repo["default_branch"]
    ref = gh("api", f"repos/{REPO}/git/ref/heads/{branch}")
    parent = ref["object"]["sha"]
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
        print(f"Installed: https://github.com/{REPO}/commit/{created['sha']}")
    if not args.skip_secret:
        save_scheduled_token(args.prompt_token)
    # Test the cloud installation without creating repair proposals or pushing.
    gh("api", "--method", "POST", f"repos/{REPO}/actions/workflows/daily-security.yml/dispatches",
       payload={"ref": branch, "inputs": {"apply": False}})
    print(f"Cloud dry run requested: https://github.com/{REPO}/actions/workflows/daily-security.yml")
    print("Review that run. Scheduled runs apply passing fixes daily at 08:00 Pacific.")


if __name__ == "__main__":
    main()
