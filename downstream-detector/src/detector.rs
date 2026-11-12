use serde_json::Value;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    time::Duration,
    time::{SystemTime, UNIX_EPOCH},
};

pub struct Config {
    pub ecosystem: String,
    pub upstream: String,
    pub baseline: String,
    pub candidate: String,
    pub crate_name: String,
    pub downstream: String,
    pub downstream_ref: String,
    pub patch_source: Option<String>,
}

// Every revision gets fresh checkouts and an independent build environment.
// This prevents artifacts from the baseline affecting the candidate.
pub fn compare(config: &Config) -> Result<String, Box<dyn std::error::Error>> {
    if !matches!(config.ecosystem.as_str(), "rust" | "python" | "npm") {
        return Err("ecosystem must be 'rust', 'python', or 'npm'".into());
    }
    if config.crate_name.is_empty()
        || !config.crate_name.chars().all(|c| {
            c.is_ascii_alphanumeric()
                || c == '-'
                || c == '_'
                || (config.ecosystem == "python" && c == '.')
                || (config.ecosystem == "npm" && matches!(c, '.' | '/' | '@'))
        })
    {
        return Err("package name contains unsupported characters for this ecosystem".into());
    }
    let root = env::temp_dir().join(format!(
        "downstream-detector-{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos()
    ));
    fs::create_dir(&root)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
    }
    let result = compare_in(config, &root);
    // The result contains the logs; temporary source trees are not retained.
    if let Err(e) = fs::remove_dir_all(&root) {
        eprintln!("warning: cannot remove {}: {e}", root.display());
    }
    result
}

fn compare_in(c: &Config, root: &Path) -> Result<String, Box<dyn std::error::Error>> {
    if c.ecosystem == "python" {
        return compare_python(c, root);
    }
    if c.ecosystem == "npm" {
        return compare_npm(c, root);
    }
    let baseline = run_revision(c, root, "baseline", &c.baseline)?;
    // Keep the captured output, but release baseline build artifacts before
    // compiling the candidate on actors with limited writable storage.
    let baseline_target = root.join("baseline/target");
    if baseline_target.exists() {
        fs::remove_dir_all(&baseline_target)?;
    }
    let candidate = run_revision(c, root, "candidate", &c.candidate)?;
    let verdict = classify(&baseline, &candidate);
    Ok(format!(
        "Verdict: {verdict}\n\nBaseline: {} (exit {})\n{}\n\nCandidate: {} (exit {})\n{}",
        c.baseline,
        status(&baseline),
        logs(&baseline),
        c.candidate,
        status(&candidate),
        logs(&candidate)
    ))
}

fn classify(baseline: &Output, candidate: &Output) -> &'static str {
    if !baseline.status.success() {
        return "INCONCLUSIVE_BASELINE_FAILED";
    }
    if candidate.status.success() {
        return "NO_REGRESSION_DETECTED";
    }
    if String::from_utf8_lossy(&candidate.stdout).contains("test result: FAILED.") {
        "POTENTIAL_REGRESSION"
    } else {
        "INCONCLUSIVE_CANDIDATE_BUILD_OR_SETUP_FAILED"
    }
}

fn run_revision(
    c: &Config,
    root: &Path,
    label: &str,
    revision: &str,
) -> Result<Output, Box<dyn std::error::Error>> {
    let dir = root.join(label);
    fs::create_dir(&dir)?;
    let upstream = checkout(&c.upstream, revision, &dir.join("upstream"))?;
    if label == "candidate" {
        if let Ok(expected) = env::var("DETECTOR_EXPECT_CANDIDATE_SHA") {
            if expected.len() != 40 || !expected.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err("invalid DETECTOR_EXPECT_CANDIDATE_SHA".into());
            }
            let mut command = Command::new("git");
            command
                .current_dir(&upstream)
                .args(["rev-parse", "--verify", "HEAD"]);
            let actual =
                crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
            let actual = String::from_utf8(actual.stdout)?.trim().to_owned();
            if !actual.eq_ignore_ascii_case(&expected) {
                return Err(format!(
                    "candidate moved during the check: expected {expected}, fetched {actual}"
                )
                .into());
            }
        }
    }
    let downstream = checkout(&c.downstream, &c.downstream_ref, &dir.join("downstream"))?;
    // Cargo's patch applies to crates.io dependencies throughout the dependency graph.
    // The checked-out crate's name and version must still satisfy downstream's constraints.
    let patch_path = upstream
        .to_string_lossy()
        .replace('\\', "\\\\")
        .replace('"', "\\\"");
    let source = c.patch_source.as_deref().unwrap_or("crates-io");
    if source.is_empty() || source.contains(['\n', '\r', '"', '\\']) {
        return Err("invalid Cargo patch source".into());
    }
    let patch = format!(
        "patch.\"{}\".{}.path=\"{}\"",
        source, c.crate_name, patch_path
    );
    let mut cargo_args = vec!["test".to_owned()];
    if !env::var("DETECTOR_CARGO_OFFLINE")
        .map(|value| value.eq_ignore_ascii_case("false"))
        .unwrap_or(false)
    {
        cargo_args.push("--offline".into());
    }
    cargo_args.extend(["--config".into(), patch]);
    let mut command = Command::new("cargo");
    command
        .current_dir(&downstream)
        .args(cargo_args)
        .env("CARGO_TARGET_DIR", dir.join("target"))
        .env_remove("DETECTOR_RUN_TOKEN");
    let output = crate::process::run_limited(&mut command, process_timeout()?, 4 * 1024 * 1024)?;
    let stderr = String::from_utf8_lossy(&output.stderr);
    if stderr.contains("was not used in the crate graph") {
        return Err(format!("Cargo did not use the {label} patch; check the crate name, source and version constraints.\n{stderr}").into());
    }
    Ok(output)
}

struct PythonRevision {
    setup_log: String,
    setup_failure: Option<String>,
    tests: Option<Output>,
}

fn compare_python(c: &Config, root: &Path) -> Result<String, Box<dyn std::error::Error>> {
    let baseline = run_python_revision(c, root, "baseline", &c.baseline)?;
    let candidate = run_python_revision(c, root, "candidate", &c.candidate)?;
    let baseline_tests_passed = baseline.tests.as_ref().is_some_and(python_tests_passed);
    let candidate_tests_passed = candidate.tests.as_ref().is_some_and(python_tests_passed);
    let verdict = if baseline.setup_failure.is_some() || !baseline_tests_passed {
        "INCONCLUSIVE_BASELINE_FAILED"
    } else if candidate.setup_failure.is_some() {
        "INCONCLUSIVE_CANDIDATE_BUILD_OR_SETUP_FAILED"
    } else if candidate_tests_passed {
        "NO_REGRESSION_DETECTED"
    } else {
        "POTENTIAL_REGRESSION"
    };
    let baseline_setup = baseline
        .setup_failure
        .as_ref()
        .map(|error| format!("{error}\n{}", baseline.setup_log))
        .unwrap_or_else(|| baseline.setup_log.clone());
    let candidate_setup = candidate
        .setup_failure
        .as_ref()
        .map(|error| format!("{error}\n{}", candidate.setup_log))
        .unwrap_or_else(|| candidate.setup_log.clone());
    Ok(format!(
        "Verdict: {verdict}\n\nBaseline setup:\n{}\nBaseline tests:\n{}\n\nCandidate setup:\n{}\nCandidate tests:\n{}",
        baseline_setup,
        baseline.tests.as_ref().map(format_python_output).unwrap_or_else(|| "not run\n".into()),
        candidate_setup,
        candidate.tests.as_ref().map(format_python_output).unwrap_or_else(|| "not run\n".into()),
    ))
}

fn python_tests_passed(output: &Output) -> bool {
    output.status.success()
        && !String::from_utf8_lossy(&output.stdout).contains("Ran 0 tests")
        && !String::from_utf8_lossy(&output.stderr).contains("Ran 0 tests")
}

fn run_python_revision(
    c: &Config,
    root: &Path,
    label: &str,
    revision: &str,
) -> Result<PythonRevision, Box<dyn std::error::Error>> {
    let dir = root.join(label);
    fs::create_dir(&dir)?;
    let upstream = checkout(&c.upstream, revision, &dir.join("upstream"))?;
    if label == "candidate" {
        if let Ok(expected) = env::var("DETECTOR_EXPECT_CANDIDATE_SHA") {
            let mut command = Command::new("git");
            command
                .current_dir(&upstream)
                .args(["rev-parse", "--verify", "HEAD"]);
            let actual =
                crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
            if !actual.status.success()
                || String::from_utf8_lossy(&actual.stdout).trim() != expected
            {
                return Err(
                    format!("candidate moved during the check: expected {expected}").into(),
                );
            }
        }
    }
    let downstream = checkout(&c.downstream, &c.downstream_ref, &dir.join("downstream"))?;
    let venv = dir.join("venv");
    let mut setup_log = String::new();
    let timeout = process_timeout()?;
    let setup =
        |command: &mut Command, stage: &str, logs: &mut String| match crate::process::run_limited(
            command,
            timeout,
            4 * 1024 * 1024,
        ) {
            Ok(output) => {
                logs.push_str(&format!(
                    "[{stage}]\n{}{}\n",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                ));
                if output.status.success() {
                    Ok(())
                } else {
                    Err(format!("{stage} failed ({}).", status(&output)))
                }
            }
            Err(error) => Err(format!("{stage} could not run: {error}")),
        };
    let mut command = Command::new("python3");
    command
        .args(["-m", "venv", "--system-site-packages"])
        .arg(&venv);
    if let Err(error) = setup(&mut command, "create virtual environment", &mut setup_log) {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(error),
            tests: None,
        });
    }
    let python = venv.join("bin/python");
    for requirement in [
        "requirements.txt",
        "requirements-test.txt",
        "test-requirements.txt",
        "requirements-dev.txt",
    ] {
        let path = downstream.join(requirement);
        if path.is_file() {
            let mut command = Command::new(&python);
            command
                .current_dir(&downstream)
                .args(["-m", "pip", "install", "--disable-pip-version-check", "-r"])
                .arg(&path);
            if let Err(error) = setup(
                &mut command,
                &format!("install {requirement}"),
                &mut setup_log,
            ) {
                return Ok(PythonRevision {
                    setup_log,
                    setup_failure: Some(error),
                    tests: None,
                });
            }
        }
    }
    if ["pyproject.toml", "setup.py", "setup.cfg"]
        .iter()
        .any(|name| downstream.join(name).is_file())
    {
        let mut command = Command::new(&python);
        command.current_dir(&downstream).args([
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            ".",
        ]);
        if let Err(error) = setup(&mut command, "install downstream project", &mut setup_log) {
            return Ok(PythonRevision {
                setup_log,
                setup_failure: Some(error),
                tests: None,
            });
        }
    }
    let mut command = Command::new(&python);
    command
        .current_dir(&downstream)
        .args([
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--force-reinstall",
        ])
        .arg(&upstream);
    if let Err(error) = setup(
        &mut command,
        "install candidate dependency source",
        &mut setup_log,
    ) {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(error),
            tests: None,
        });
    }
    let mut pytest_probe = Command::new(&python);
    pytest_probe.args(["-c", "import pytest"]);
    let has_pytest = crate::process::run_limited(&mut pytest_probe, Duration::from_secs(10), 1024)?
        .status
        .success();
    let declares_pytest = [
        "pyproject.toml",
        "setup.cfg",
        "requirements.txt",
        "requirements-test.txt",
        "test-requirements.txt",
        "requirements-dev.txt",
    ]
    .iter()
    .filter_map(|name| fs::read_to_string(downstream.join(name)).ok())
    .any(|text| text.to_ascii_lowercase().contains("pytest"));
    if declares_pytest && !has_pytest {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some("pytest is declared by the downstream project but is unavailable in the detector image or test dependencies".into()),
            tests: None,
        });
    }
    let mut command = Command::new(&python);
    command.current_dir(&downstream);
    if has_pytest {
        command.args(["-m", "pytest", "-q"]);
    } else {
        command.args(["-m", "unittest", "discover", "-v"]);
    }
    let tests = crate::process::run_limited(&mut command, process_timeout()?, 4 * 1024 * 1024)?;
    Ok(PythonRevision {
        setup_log,
        setup_failure: None,
        tests: Some(tests),
    })
}

fn format_python_output(output: &Output) -> String {
    format!(
        "exit {}\n{}{}",
        status(output),
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn compare_npm(c: &Config, root: &Path) -> Result<String, Box<dyn std::error::Error>> {
    let baseline = run_npm_revision(c, root, "baseline", &c.baseline)?;
    let candidate = run_npm_revision(c, root, "candidate", &c.candidate)?;
    let baseline_passed = baseline
        .tests
        .as_ref()
        .is_some_and(|output| output.status.success());
    let candidate_passed = candidate
        .tests
        .as_ref()
        .is_some_and(|output| output.status.success());
    let verdict = if baseline.setup_failure.is_some() || !baseline_passed {
        "INCONCLUSIVE_BASELINE_FAILED"
    } else if candidate.setup_failure.is_some() {
        "INCONCLUSIVE_CANDIDATE_BUILD_OR_SETUP_FAILED"
    } else if candidate_passed {
        "NO_REGRESSION_DETECTED"
    } else {
        "POTENTIAL_REGRESSION"
    };
    let baseline_setup = baseline
        .setup_failure
        .as_ref()
        .map(|error| format!("{error}\n{}", baseline.setup_log))
        .unwrap_or_else(|| baseline.setup_log.clone());
    let candidate_setup = candidate
        .setup_failure
        .as_ref()
        .map(|error| format!("{error}\n{}", candidate.setup_log))
        .unwrap_or_else(|| candidate.setup_log.clone());
    Ok(format!(
        "Verdict: {verdict}\n\nBaseline setup:\n{}\nBaseline tests:\n{}\n\nCandidate setup:\n{}\nCandidate tests:\n{}",
        baseline_setup,
        baseline.tests.as_ref().map(format_python_output).unwrap_or_else(|| "not run\n".into()),
        candidate_setup,
        candidate.tests.as_ref().map(format_python_output).unwrap_or_else(|| "not run\n".into()),
    ))
}

fn run_npm_revision(
    c: &Config,
    root: &Path,
    label: &str,
    revision: &str,
) -> Result<PythonRevision, Box<dyn std::error::Error>> {
    let dir = root.join(label);
    fs::create_dir(&dir)?;
    let upstream = checkout(&c.upstream, revision, &dir.join("upstream"))?;
    if label == "candidate" {
        if let Ok(expected) = env::var("DETECTOR_EXPECT_CANDIDATE_SHA") {
            let mut command = Command::new("git");
            command
                .current_dir(&upstream)
                .args(["rev-parse", "--verify", "HEAD"]);
            let actual =
                crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
            if !actual.status.success()
                || String::from_utf8_lossy(&actual.stdout).trim() != expected
            {
                return Err(
                    format!("candidate moved during the check: expected {expected}").into(),
                );
            }
        }
    }
    let downstream = checkout(&c.downstream, &c.downstream_ref, &dir.join("downstream"))?;
    let mut setup_log = String::new();
    let timeout = process_timeout()?;
    let setup =
        |command: &mut Command, stage: &str, logs: &mut String| match crate::process::run_limited(
            command,
            timeout,
            4 * 1024 * 1024,
        ) {
            Ok(output) => {
                logs.push_str(&format!(
                    "[{stage}]\n{}{}\n",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                ));
                if output.status.success() {
                    Ok(())
                } else {
                    Err(format!("{stage} failed ({}).", status(&output)))
                }
            }
            Err(error) => Err(format!("{stage} could not run: {error}")),
        };
    let manifest_path = downstream.join("package.json");
    let manifest: Value = match fs::read_to_string(&manifest_path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
    {
        Some(manifest) => manifest,
        None => {
            return Ok(PythonRevision {
                setup_log,
                setup_failure: Some("consumer repository has no valid root package.json".into()),
                tests: None,
            });
        }
    };
    if manifest.get("workspaces").is_some() {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some("npm workspaces are not supported yet; the consumer must have a root package.json without workspaces".into()),
            tests: None,
        });
    }
    if manifest["scripts"]["test"].as_str().is_none() {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some("consumer package.json has no scripts.test command".into()),
            tests: None,
        });
    }
    let manager = manifest["packageManager"].as_str().unwrap_or("npm");
    if !manager.starts_with("npm@") && manager != "npm" {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(format!(
                "unsupported package manager '{manager}'; this runner currently supports npm"
            )),
            tests: None,
        });
    }
    if downstream.join("pnpm-lock.yaml").exists() || downstream.join("yarn.lock").exists() {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(
                "pnpm and Yarn lockfiles are not supported yet; this runner currently supports npm"
                    .into(),
            ),
            tests: None,
        });
    }
    let mut install = Command::new("npm");
    install.current_dir(&downstream).args([
        if downstream.join("package-lock.json").exists()
            || downstream.join("npm-shrinkwrap.json").exists()
        {
            "ci"
        } else {
            "install"
        },
        "--no-audit",
        "--no-fund",
    ]);
    if let Err(error) = setup(
        &mut install,
        "install downstream dependencies",
        &mut setup_log,
    ) {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(error),
            tests: None,
        });
    }
    let mut install_upstream = Command::new("npm");
    install_upstream
        .current_dir(&downstream)
        .args([
            "install",
            "--no-save",
            "--package-lock=false",
            "--force",
            "--no-audit",
            "--no-fund",
        ])
        .arg(&upstream);
    if let Err(error) = setup(
        &mut install_upstream,
        "install changed package source",
        &mut setup_log,
    ) {
        return Ok(PythonRevision {
            setup_log,
            setup_failure: Some(error),
            tests: None,
        });
    }
    let mut test = Command::new("npm");
    test.current_dir(&downstream).arg("test");
    let test_output = crate::process::run_limited(&mut test, process_timeout()?, 4 * 1024 * 1024)?;
    Ok(PythonRevision {
        setup_log,
        setup_failure: None,
        tests: Some(test_output),
    })
}

fn checkout(
    repository: &str,
    revision: &str,
    destination: &Path,
) -> Result<PathBuf, Box<dyn std::error::Error>> {
    // '--' prevents a repository name being interpreted as a git option.
    let mut command = git_command()?;
    command
        .args(["clone", "--quiet", "--no-hardlinks", "--", repository])
        .env("GIT_TERMINAL_PROMPT", "0")
        .arg(destination);
    let clone = crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
    if !clone.status.success() {
        return Err(format!(
            "git clone failed: {}",
            String::from_utf8_lossy(&clone.stderr)
        )
        .into());
    }
    if revision.is_empty() || revision.starts_with('-') || revision.contains(['\n', '\r']) {
        return Err("invalid Git revision".into());
    }
    let resolve = |reference: &str| -> Result<Output, Box<dyn std::error::Error>> {
        let mut command = Command::new("git");
        command.current_dir(destination).args([
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{reference}^{{commit}}"),
        ]);
        Ok(crate::process::run_limited(
            &mut command,
            process_timeout()?,
            1024 * 1024,
        )?)
    };
    let mut resolved = resolve(revision)?;
    if !resolved.status.success() {
        let mut command = git_command()?;
        command
            .current_dir(destination)
            .args(["fetch", "--quiet", "origin", revision])
            .env("GIT_TERMINAL_PROMPT", "0");
        let fetch = crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
        if fetch.status.success() {
            resolved = resolve("FETCH_HEAD")?;
        }
    }
    if !resolved.status.success() {
        return Err(format!(
            "invalid revision {revision}: {}",
            String::from_utf8_lossy(&resolved.stderr)
        )
        .into());
    }
    let commit = String::from_utf8(resolved.stdout)?.trim().to_owned();
    let mut command = Command::new("git");
    command
        .current_dir(destination)
        .args(["checkout", "--quiet", "--detach", &commit]);
    let checkout = crate::process::run_limited(&mut command, process_timeout()?, 1024 * 1024)?;
    if !checkout.status.success() {
        return Err(format!(
            "git checkout failed for {revision}: {}",
            String::from_utf8_lossy(&checkout.stderr)
        )
        .into());
    }
    Ok(fs::canonicalize(destination)?)
}

fn process_timeout() -> Result<Duration, Box<dyn std::error::Error>> {
    let seconds = env::var("DETECTOR_PROCESS_TIMEOUT_SECS")
        .unwrap_or_else(|_| "600".into())
        .parse::<u64>()?;
    if seconds == 0 || seconds > 3600 {
        return Err("DETECTOR_PROCESS_TIMEOUT_SECS must be between 1 and 3600".into());
    }
    Ok(Duration::from_secs(seconds))
}

fn git_command() -> Result<Command, Box<dyn std::error::Error>> {
    let mut command = Command::new("git");
    command.arg("-c").arg("http.followRedirects=false");
    if let Ok(host) = env::var("DETECTOR_GIT_AUTH_HOST") {
        if host.is_empty()
            || !host
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-')
        {
            return Err("invalid DETECTOR_GIT_AUTH_HOST".into());
        }
        // The credential proxy replaces this placeholder. The authorization
        // policy is host-scoped and must use a short-lived read-only token.
        command.args([
            "-c",
            &format!("http.https://{host}/.extraHeader=Authorization: Bearer placeholder"),
        ]);
    }
    Ok(command)
}

fn status(output: &Output) -> String {
    output
        .status
        .code()
        .map(|n| n.to_string())
        .unwrap_or_else(|| "signal".into())
}
fn logs(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}
