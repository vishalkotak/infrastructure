# Daily GitHub security repairs

Controller for active, non-fork repositories owned by **vishalkotak**, including
new repositories discovered on subsequent runs. Forks are excluded from scans,
repairs, pushes, and installer alert-access checks. Repositories with missing fork
metadata are also excluded. Renovate's fork processing is disabled, and the
publisher independently rejects forks. Hosted by GitHub Actions in
`vishalkotak/infrastructure`, daily at **08:00 America/Los_Angeles**, with automatic
daylight-saving adjustment. Your Mac can be asleep or off. GitHub may delay or
drop scheduled runs; this is not an exact-time or guaranteed-delivery scheduler.
GitHub can disable schedules in public repositories after 60 days without
repository activity; check the Actions page periodically.

## Activation

The source files are prepared locally. The current ChatGPT GitHub integration
rejected uploads with HTTP 403 (`Resource not accessible by integration`). The
user completed GitHub CLI login, but this agent's sandbox cannot reach GitHub;
its authentication-status failure does not establish that the login is invalid.
Nothing has been deployed by this session. In the terminal where GitHub CLI
login succeeded, run:

```sh
python3 /Users/vishal/code/github-security-bot/install.py
```

If upload and secret setup succeeded but GitHub returned HTTP 500 starting the
first run, resume just the cloud validation with:

```sh
python3 /Users/vishal/code/github-security-bot/install.py --resume
```

The installer retries temporary dispatch failures and checks for a run created
despite an error before retrying. Test fixture Git output is suppressed on
successful installation; those tests only create disposable local repositories.
It also verifies the run's `head_sha` matches the deployed revision. If GitHub
dispatches an older revision while indexing a new commit, the installer cancels
that specific stale run and retries; it does not report the old run as validation
of the new code.

The installer runs the tests, checks workflow authorization (requesting the
additional `workflow` and `security_events` OAuth scopes through GitHub only if
needed), uploads the files in one
commit, saves the existing authenticated CLI credential directly as the Actions
secret `SECURITY_BOT_TOKEN`, and requests a cloud dry run. It never prints the
token, saves it in source files, or puts it in a command-line argument. This
reuses the CLI credential's permissions for unattended runs; revoking that
credential also stops the bot. Repository protection rules can reject
installation; the installer does not disable them.

Before saving a scheduled token it verifies that token can list Dependabot
alerts in every discovered active repository. If GitHub explicitly reports that
alerts are disabled, it enables vulnerability alerts and verifies access again.
Other HTTP 403 responses remain errors. This distinguishes a disabled feature
from missing authorization and prevents another cloud run with unreadable alerts.

For a separate token with narrower permissions, use `install.py --prompt-token`.
To preserve a previously configured secret, use `install.py --skip-secret`.
The instructions below describe the optional separate token.

To deploy an update using the existing credential and immediately exercise live
repairs without review approvals:

```sh
python3 /Users/vishal/code/github-security-bot/install.py --skip-secret --apply
```

Build/audit/test failures still block publication. Temporary repair branches and
PRs provide an audit trail; passing fixes are published directly to the default
branch by the controller. Renovate's own automerge stays disabled so it cannot
publish a change before the controller tests its exact merge commit.

The controller needs a persistent credential in GitHub Actions. The interactive
ChatGPT GitHub connection is not a credential a scheduled workflow can reuse.

1. Create a fine-grained GitHub personal access token owned by `vishalkotak`,
   selecting **All repositories**. Grant repository permissions:
   **Contents: read and write**, **Pull requests: read and write**,
   **Dependabot alerts: read**, **Checks: read**, and **Commit statuses: read**.
   Metadata read is implicit. No administration or workflow-write permission is
   requested. Pick an expiration you can track and rotate before it expires.
2. Add it as the Actions secret **SECURITY_BOT_TOKEN** in
   https://github.com/vishalkotak/infrastructure/settings/secrets/actions
   Do not paste the token into chat, a tracked file, or an issue.
3. Enable Dependency graph and Dependabot alerts in repository security settings
   where available. The controller reports inaccessible/disabled alerts as
   coverage gaps. Renovate also queries OSV for supported direct dependencies.
4. Run **Daily security repairs** manually with `apply` unchecked first; inspect
   the Actions summary and report. To exercise publication, run with `apply`
   checked. Scheduled executions apply passing repairs automatically.
5. Ensure Actions failure notifications are enabled in your GitHub notification
   settings if you want email on blocked runs. This project does not configure
   your email or send a separate daily email digest.

Local use requires Python 3.12+, Git, and Docker:

```sh
python3 -m unittest -v
# Set SECURITY_BOT_TOKEN through your shell's secure credential mechanism.
python3 bot.py                  # dry-run: no proposals or pushes
python3 bot.py --apply          # create repair proposals and publish passing ones
python3 bot.py --skip-renovate  # validate already-open proposals only
```

## Behavior and limits

This implementation fixes **known dependency vulnerabilities** in npm, Python,
and Cargo projects. Renovate creates security-only repair pull requests in
`security-bot/*`; regular version upgrades are disabled. It does not scan source
code for arbitrary logic flaws, scan for leaked secrets, rotate credentials, or
apply AI-generated source patches. Those require additional scanners and a
separate remediation design. Dependencies unsupported by these managers are not
automatically repaired. A repo with no dependency manifests has no dependency
repair work, and is not certified secure by this tool.

The controller scans GitHub Dependabot alerts, considers only its own marked
security proposals, and rejects edits outside supported manifests and lockfiles.
One proposal per repository is considered per day. Vulnerability repairs are
grouped where Renovate supports it. Existing pending, failed, skipped, or neutral
CI checks block publication. Missing tests are not interpreted as passing tests.

The candidate is merged with the current default branch locally. The exact
resulting commit is saved outside the test container before testing:

- npm: first repair remaining transitive findings with `npm audit fix
  --package-lock-only --ignore-scripts` in a separate directory containing only
  the manifests. Declared dependency ranges must stay unchanged. Commit the
  resulting lockfile before validation, then `npm ci --ignore-scripts`, a clean `npm audit`, and available build/test
  scripts. A project with neither test nor build fails validation.
- Python: choose Python 3.13 or newer when the manifest requires it; otherwise
  default to 3.12. Install using Poetry, uv, or pip; a clean `pip-audit`; and pytest.
  Poetry dependency environments use `--no-root` to support repositories that
  are collections of scripts rather than installable packages; source roots are
  placed on `PYTHONPATH` for testing.
  No tests collected, unsupported project setup, or dependency installation
  failures block publication. Multiple requirements files are conservatively
  installed together; projects with incompatible environment-specific files
  need a dedicated validator.
- Rust: a clean `cargo audit` and `cargo test --workspace --locked`.

Validation uses 15-minute budgets per phase and CPU containers with limited
memory. GPU projects such as vLLM, very large workspaces such as DataFusion,
private package registries, system dependencies, and unusual test layouts may
need dedicated validators before their repairs can pass. Existing vulnerabilities
in a tested environment can also block an otherwise useful partial fix.

Repository code receives no GitHub credential, SSH agent, host home directory,
controller files, or Docker socket. Package installation and tests run in
disposable containers; Renovate gets its token in a separate container with
package scripts and custom commands disabled. Official Actions are commit-pinned;
container images are version-tagged and should be periodically reviewed/updated.

After tests pass, a **normal fast-forward push** puts the tested merge commit on
the repository's current default branch (`main`, `master`, etc.). A concurrent
branch edit rejects the push. Existing rulesets and branch protection apply;
the controller does not change or bypass them. Protected repositories may require
a different merge integration. The repair PR is retained as an audit trail and
GitHub normally marks it merged when its commit reaches the base branch.

After publication the controller fetches the remote branch, verifies it contains
the tested commit, and repeats audit/build/tests from a fresh checkout. It also
reports the published commit's currently available CI state. A later asynchronous
CI failure is handled by that workflow's own GitHub notifications; this process
does not monitor indefinitely. A post-publication failure fails this workflow;
automatic rollback is not implemented. Alert closure may lag dependency-graph
processing and is checked again on the next daily scan.

The Actions artifact `daily-security-report` records findings, proposal links,
blocked repairs, pushed commit IDs, and post-publication validation results.
Reports never contain token values or secret-scanning matches. Any alert API
permission errors are reported as coverage gaps, not clean scans.

References: [Renovate security updates](https://docs.renovatebot.com/presets-security/),
[Renovate configuration](https://docs.renovatebot.com/configuration-options/),
[GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
