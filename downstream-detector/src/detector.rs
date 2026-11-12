use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    time::Duration,
    time::{SystemTime, UNIX_EPOCH},
};

pub struct Config {
    pub upstream: String,
    pub baseline: String,
    pub candidate: String,
    pub crate_name: String,
    pub downstream: String,
    pub downstream_ref: String,
    pub patch_source: Option<String>,
}

// Every comparison gets new checkouts and independent Cargo target directories.
// This prevents build artifacts from the baseline affecting the candidate.
pub fn compare(config: &Config) -> Result<String, Box<dyn std::error::Error>> {
    if config.crate_name.is_empty()
        || !config
            .crate_name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err("crate name must contain only letters, digits, '-' or '_'".into());
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
