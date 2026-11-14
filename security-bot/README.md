# security-bot

Fixes known vulnerable dependencies across all my non-fork repos, daily, without me.

GitHub Actions runs it at 08:00 Pacific ([workflow](../.github/workflows/daily-security.yml)).

## How it works

1. Read Dependabot alerts for every active repo.
2. Renovate opens a security-only PR (`security-bot/*`). No routine upgrades.
3. Merge it with the default branch locally and pin that exact commit.
4. Validate in a disposable, memory-limited container:
   - npm: `npm audit fix --package-lock-only`, `npm ci`, clean audit, build/test
   - Python: Poetry, uv or pip install, clean `pip-audit`, pytest
   - Rust: clean `cargo audit`, `cargo test --workspace --locked`
5. Fast-forward push the tested commit. Concurrent edits reject the push.
6. Re-run everything from a fresh checkout of the remote branch.

Every run uploads a `daily-security-report` artifact.

## Guardrails

- Only manifests and lockfiles may change. Declared ranges stay put.
- Failing, pending or missing checks block publication. No tests is not passing.
- Repo code never sees the token, SSH agent, home dir or Docker socket.
- Renovate runs in its own container with package scripts disabled.
- Branch protection is respected, never bypassed.
- Alert API errors are reported as coverage gaps, not clean scans.

## Not covered

Source-level bugs, leaked secrets, and anything outside npm, Python and Cargo. GPU code and very large workspaces need dedicated validators. No automatic rollback.

## Setup

Needs Python 3.12+, Git, Docker, and a fine-grained token stored as the `SECURITY_BOT_TOKEN` Actions secret. The token needs read/write on contents and pull requests, plus read on Dependabot alerts, checks and statuses.

```sh
python3 install.py                 # test, upload, store current gh token, dry run
python3 install.py --prompt-token  # use a separate, narrower token
python3 install.py --skip-secret   # keep the existing secret
python3 install.py --resume        # retry only the cloud validation

python3 bot.py                     # dry run
python3 bot.py --apply             # open and publish passing repairs
python3 -m unittest -v
```
