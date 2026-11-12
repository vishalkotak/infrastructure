use std::{
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Output},
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
    let output = Command::new("cargo")
        .current_dir(&downstream)
        .args(["test", "--offline", "--config", &patch])
        .env("CARGO_TARGET_DIR", dir.join("target"))
        .output()?;
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
    let mut command = Command::new("git");
    if let Ok(host) = env::var("DETECTOR_GIT_AUTH_HOST") {
        if host.is_empty()
            || !host
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-')
        {
            return Err("invalid DETECTOR_GIT_AUTH_HOST".into());
        }
        // This is not a secret. The Substrate egress gateway replaces it with
        // the real Authorization header for the configured host.
        command.args([
            "-c",
            &format!("http.https://{host}/.extraHeader=Authorization: placeholder"),
        ]);
    }
    let clone = command
        .args(["clone", "--quiet", "--no-hardlinks", "--", repository])
        .env("GIT_TERMINAL_PROMPT", "0")
        .arg(destination)
        .output()?;
    if !clone.status.success() {
        return Err(format!(
            "git clone failed: {}",
            String::from_utf8_lossy(&clone.stderr)
        )
        .into());
    }
    let resolved = Command::new("git")
        .current_dir(destination)
        .args([
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{revision}^{{commit}}"),
        ])
        .output()?;
    if !resolved.status.success() {
        return Err(format!(
            "invalid revision {revision}: {}",
            String::from_utf8_lossy(&resolved.stderr)
        )
        .into());
    }
    let commit = String::from_utf8(resolved.stdout)?.trim().to_owned();
    let checkout = Command::new("git")
        .current_dir(destination)
        .args(["checkout", "--quiet", "--detach", &commit])
        .output()?;
    if !checkout.status.success() {
        return Err(format!(
            "git checkout failed for {revision}: {}",
            String::from_utf8_lossy(&checkout.stderr)
        )
        .into());
    }
    Ok(fs::canonicalize(destination)?)
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
