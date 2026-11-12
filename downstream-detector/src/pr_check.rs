use serde_json::{json, Value};
use std::{collections::BTreeMap, env, error::Error, io::Write, process::Command};

struct PullRequest {
    host: String,
    owner: String,
    repo: String,
    number: u64,
    base_sha: String,
    head_sha: String,
}

struct Dependent {
    repository: String,
    clone_url: String,
    revision: String,
    priority: u64,
}

type RepositoryDiscovery = (Vec<Dependent>, Vec<Value>, usize);

pub struct CheckResult {
    pub summary: String,
    pub succeeded: bool,
}

pub fn check(pr_url: &str) -> Result<CheckResult, Box<dyn Error>> {
    let (host, owner, repo, number) = parse_pr_url(pr_url)?;
    let approved_enterprise_host = env::var("DETECTOR_GITHUB_HOST").ok();
    if !approved_github_host(&host, approved_enterprise_host.as_deref()) {
        return Err(format!(
            "untrusted GitHub host '{host}'; set DETECTOR_GITHUB_HOST to the approved GitHub Enterprise host"
        ).into());
    }
    let api = api_root(&host);
    let pr_body = get(&format!("{api}/repos/{owner}/{repo}/pulls/{number}"))?;
    let pr_json: Value = serde_json::from_str(&pr_body)?;
    let pr = PullRequest {
        host,
        owner,
        repo,
        number,
        base_sha: sha(&pr_json["base"]["sha"], "PR base SHA")?.to_owned(),
        head_sha: sha(&pr_json["head"]["sha"], "PR head SHA")?.to_owned(),
    };

    let (ecosystem, package_name) = changed_package(&pr, &api)?;
    validate_package_name(&ecosystem, &package_name)?;
    let max_jobs = env::var("DETECTOR_MAX_JOBS")
        .unwrap_or_else(|_| "5".into())
        .parse::<usize>()?;
    if max_jobs == 0 || max_jobs > 32 {
        return Err("DETECTOR_MAX_JOBS must be between 1 and 32".into());
    }
    let lookup_budget = env::var("DETECTOR_MAX_REPOSITORY_LOOKUPS")
        .unwrap_or_else(|_| "50".into())
        .parse::<usize>()?;
    if lookup_budget == 0 || lookup_budget > 1000 {
        return Err("DETECTOR_MAX_REPOSITORY_LOOKUPS must be between 1 and 1000".into());
    }
    let (dependents, skipped, discovered_repositories, public_discovery_issue) = if ecosystem
        == "rust"
    {
        let user_agent = env::var("CRATES_IO_USER_AGENT").map_err(|_| {
            "set CRATES_IO_USER_AGENT once with an identifying name and contact before using check"
        })?;
        let (discovery, public_issue): (Value, Option<String>) = match crate::discovery::discover(
            &package_name,
            &user_agent,
        ) {
            Ok(body) => (serde_json::from_str(&body)?, None),
            Err(error)
                if env::var("DETECTOR_GITHUB_ORG").is_ok() && error.to_string().contains("404") =>
            {
                (json!({"upstream":package_name,"dependents":[]}), None)
            }
            Err(error) if env::var("DETECTOR_GITHUB_ORG").is_ok() => {
                eprintln!("crates.io discovery incomplete ({error}); checking configured company repositories but marking coverage incomplete");
                (
                    json!({"upstream":package_name,"dependents":[]}),
                    Some(error.to_string()),
                )
            }
            Err(error) => return Err(error),
        };
        let plan = crate::plan::rank(&discovery.to_string())?;
        let company_budget = if plan.repositories.is_empty() {
            lookup_budget
        } else {
            lookup_budget.saturating_sub(max_jobs).max(1)
        };
        let (company, mut skipped, company_lookups) =
            discover_company_repositories(&package_name, max_jobs, company_budget)?;
        let mut dependents = company;
        if let Some(issue) = &public_issue {
            skipped.push(json!({"source":"crates.io", "reason":issue}));
        }
        let mut lookups = company_lookups;
        for (repository, downloads, _) in &plan.repositories {
            if dependents.len() >= max_jobs || lookups >= lookup_budget {
                break;
            }
            let clone_url = format!("{repository}.git");
            if dependents.iter().any(|dep| dep.clone_url == clone_url) {
                continue;
            }
            lookups += 1;
            match latest_revision(repository, *downloads, "github.com") {
                Ok(dep) => dependents.push(dep),
                Err(error) => {
                    skipped.push(json!({"repository":repository,"reason":error.to_string()}))
                }
            }
        }
        (
            dependents,
            skipped,
            plan.eligible_repositories,
            public_issue,
        )
    } else {
        let (dependents, skipped, discovered) =
            discover_python_repositories(&package_name, max_jobs, lookup_budget)?;
        (dependents, skipped, discovered, None)
    };
    if dependents.is_empty() {
        return Err(format!("no runnable downstream repositories found for {package_name}").into());
    }
    let planned_jobs: Vec<Value> = dependents
        .iter()
        .map(|d| {
            json!({
                "repository":d.repository,
                "clone_url":d.clone_url,
                "revision":d.revision,
                "priority":d.priority,
            })
        })
        .collect();
    if env::var("DETECTOR_DRY_RUN").is_ok_and(|value| value == "true") {
        return Ok(CheckResult {
            summary: serde_json::to_string_pretty(&json!({
            "pull_request":pr_url,
            "ecosystem":ecosystem,
            "package":package_name,
            "baseline":pr.base_sha,
            "candidate":pr.head_sha,
            "discovered_repositories":discovered_repositories,
            "selected_repositories":dependents.len(),
            "skipped_repositories":skipped,
            "coverage_complete":ecosystem == "rust" && public_discovery_issue.is_none(),
            "discovery_source":if ecosystem == "rust" { "crates.io reverse dependencies" } else { "GitHub code search sample; not exhaustive" },
            "planned_jobs":planned_jobs,
            }))?,
            succeeded: false,
        });
    }

    let executor = env::var("DETECTOR_EXECUTOR").unwrap_or_else(|_| "substrate".into());
    let parallel = env::var("DETECTOR_MAX_PARALLEL")
        .unwrap_or_else(|_| "2".into())
        .parse::<usize>()?;
    if parallel == 0 || parallel > 32 {
        return Err("DETECTOR_MAX_PARALLEL must be between 1 and 32".into());
    }
    let mut config = json!({
        "upstream":format!("https://{}/{}/{}/.git",pr.host,pr.owner,pr.repo),
        "baseline":pr.base_sha,
        "candidate":format!("refs/pull/{}/head",pr.number),
        "candidate_sha":pr.head_sha,
        "crate_name":package_name,
        "ecosystem":ecosystem,
        "max_jobs":max_jobs,
        "max_parallel":parallel,
        "cargo_offline":false,
        "jobs":dependents.iter().map(|d|json!({"repository":d.clone_url,"revision":d.revision,"priority":d.priority})).collect::<Vec<_>>(),
    });
    match executor.as_str() {
        "substrate" => {
            let image = env::var("DETECTOR_IMAGE").map_err(|_| {
                "set DETECTOR_IMAGE to a digest-pinned detector image once during setup"
            })?;
            if !image.contains("@sha256:") {
                return Err("DETECTOR_IMAGE must use an immutable @sha256: digest".into());
            }
            config["executor"] = json!({"type":"substrate"});
            config["image"] = json!(image);
            config["atespace"] =
                json!(env::var("DETECTOR_ATESPACE").unwrap_or_else(|_| "compatibility".into()));
            let auth_host = env::var("DETECTOR_GIT_AUTH_HOST")
                .ok()
                .or_else(|| {
                    env::var("DETECTOR_GITHUB_HOST")
                        .ok()
                        .filter(|host| host != "github.com")
                })
                .or_else(|| (pr.host != "github.com").then(|| pr.host.clone()));
            if let Some(auth_host) = auth_host {
                let policy = env::var("DETECTOR_EGRESS_POLICY").map_err(|_| {
                    "authenticated Git access requires DETECTOR_EGRESS_POLICY with a host-scoped HTTPS credential rule"
                })?;
                config["git_auth_host"] = json!(auth_host);
                config["egress_policy"] = json!(policy);
            } else {
                config["egress_policy"] = json!(env::var("DETECTOR_EGRESS_POLICY")
                    .unwrap_or_else(|_| "substrate/egress-policy.yaml".into()));
            }
        }
        "command" => {
            let command = env::var("DETECTOR_EXECUTOR_COMMAND")
                .map_err(|_| "set DETECTOR_EXECUTOR_COMMAND to your trusted executor adapter")?;
            config["executor"] = json!({"type":"command","command":command});
            if let Ok(auth_host) = env::var("DETECTOR_GIT_AUTH_HOST") {
                config["git_auth_host"] = json!(auth_host);
            } else if let Ok(company_host) = env::var("DETECTOR_GITHUB_HOST") {
                config["git_auth_host"] = json!(company_host);
            } else if pr.host != "github.com" {
                config["git_auth_host"] = json!(pr.host);
            }
        }
        _ => return Err("DETECTOR_EXECUTOR must be 'substrate' or 'command'".into()),
    }
    let results: Value = serde_json::from_str(&crate::batch::run(&config.to_string())?)?;
    let full_report = json!({
        "pull_request":pr_url,
        "ecosystem":ecosystem,
        "package":package_name,
        "baseline":pr.base_sha,
        "candidate":pr.head_sha,
        "discovered_repositories":discovered_repositories,
        "selected_repositories":dependents.len(),
        "skipped_repositories":skipped,
        "coverage_complete":ecosystem == "rust" && public_discovery_issue.is_none(),
        "discovery_source":if ecosystem == "rust" { "crates.io reverse dependencies" } else { "GitHub code search sample; not exhaustive" },
        "results":results,
    });
    let report_dir =
        env::var("DETECTOR_REPORT_DIR").unwrap_or_else(|_| ".downstream-detector/reports".into());
    std::fs::create_dir_all(&report_dir)?;
    let run_stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let report_path = std::path::Path::new(&report_dir).join(format!(
        "{}-{}-pr-{}-{}-{}.json",
        pr.owner,
        pr.repo,
        pr.number,
        &pr.head_sha[..12],
        run_stamp
    ));
    write_private_report(&report_path, &serde_json::to_vec_pretty(&full_report)?)?;

    let runs = full_report["results"]["results"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut summary = format!(
        "PR check: {pr_url}\nPackage: {package_name} ({ecosystem})\nDownstream repositories tested: {}\n",
        runs.len()
    );
    if ecosystem == "python" {
        summary.push_str("Discovery: GitHub code-search sample; not exhaustive.\n");
    }
    let mut succeeded = public_discovery_issue.is_none();
    if let Some(issue) = public_discovery_issue {
        summary.push_str(&format!(
            "Coverage incomplete: crates.io discovery failed: {issue}\n"
        ));
    }
    for run in runs {
        let repository = run["repository"].as_str().unwrap_or("unknown repository");
        let result = &run["result"];
        let outcome = if result["status"] == "completed" {
            let verdict = result["report"]
                .as_str()
                .and_then(|report| report.lines().find(|line| line.starts_with("Verdict:")))
                .unwrap_or("Verdict: MISSING_OR_INVALID")
                .to_owned();
            if verdict != "Verdict: NO_REGRESSION_DETECTED" {
                succeeded = false;
            }
            verdict
        } else {
            succeeded = false;
            format!(
                "ERROR: {}",
                result["error"].as_str().unwrap_or("unknown error")
            )
        };
        summary.push_str(&format!("  {repository}: {outcome}\n"));
    }
    summary.push_str(&format!("Full logs: {}", report_path.display()));
    Ok(CheckResult { summary, succeeded })
}

fn write_private_report(path: &std::path::Path, contents: &[u8]) -> std::io::Result<()> {
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path)?;
    file.write_all(contents)
}

fn approved_github_host(host: &str, enterprise_host: Option<&str>) -> bool {
    host == "github.com" || enterprise_host == Some(host)
}

fn token_env_for_api_host(api_host: &str, enterprise_host: Option<&str>) -> Option<&'static str> {
    if api_host == "api.github.com" {
        Some("GITHUB_TOKEN")
    } else if enterprise_host == Some(api_host) {
        Some("DETECTOR_GHES_TOKEN")
    } else {
        None
    }
}

fn parse_pr_url(input: &str) -> Result<(String, String, String, u64), Box<dyn Error>> {
    let rest = input
        .strip_prefix("https://")
        .ok_or("PR URL must use HTTPS")?;
    let (host, path) = rest.split_once('/').ok_or("invalid PR URL")?;
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-')
    {
        return Err("invalid GitHub host in PR URL".into());
    }
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() != 4 || parts[2] != "pull" {
        return Err("expected https://host/owner/repository/pull/number".into());
    }
    let clean = |s: &str| {
        !s.is_empty()
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    };
    if !clean(parts[0]) || !clean(parts[1]) {
        return Err("invalid repository owner or name in PR URL".into());
    }
    let number = parts[3]
        .split(['?', '#'])
        .next()
        .ok_or("missing pull request number")?
        .parse::<u64>()?;
    if number == 0 {
        return Err("pull request number must be positive".into());
    }
    Ok((
        host.to_ascii_lowercase(),
        parts[0].into(),
        parts[1].into(),
        number,
    ))
}

fn api_root(host: &str) -> String {
    if host == "github.com" {
        "https://api.github.com".into()
    } else {
        format!("https://{host}/api/v3")
    }
}

fn get(url: &str) -> Result<String, Box<dyn Error>> {
    get_accept(url, "application/vnd.github+json")
}

fn get_raw(url: &str) -> Result<String, Box<dyn Error>> {
    get_accept(url, "application/vnd.github.raw+json")
}

fn get_accept(url: &str, accept: &str) -> Result<String, Box<dyn Error>> {
    let mut command = Command::new("curl");
    command.args([
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--max-time",
        "60",
        "--max-filesize",
        "33554432",
        "--retry",
        "2",
        "--retry-connrefused",
        "--retry-delay",
        "1",
        "--header",
        &format!("Accept: {accept}"),
    ]);
    command.arg(url);
    let api_host = url
        .strip_prefix("https://")
        .and_then(|rest| rest.split_once('/').map(|(host, _)| host))
        .ok_or("GitHub API URL must use HTTPS")?;
    let token =
        match token_env_for_api_host(api_host, env::var("DETECTOR_GITHUB_HOST").ok().as_deref()) {
            Some("GITHUB_TOKEN") => env::var("GITHUB_TOKEN")
                .or_else(|_| env::var("GH_TOKEN"))
                .ok(),
            Some("DETECTOR_GHES_TOKEN") => env::var("DETECTOR_GHES_TOKEN").ok(),
            _ => None,
        };
    let output = if let Some(token) = token.filter(|token| !token.is_empty()) {
        if token.trim().is_empty()
            || !token
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        {
            return Err("configured GitHub token contains invalid characters".into());
        }
        command.args(["--config", "-"]);
        crate::process::run_with_stdin(
            &mut command,
            format!("header = \"Authorization: Bearer {token}\"\n").as_bytes(),
            std::time::Duration::from_secs(60),
            32 * 1024 * 1024,
        )?
    } else {
        crate::process::run_limited(
            &mut command,
            std::time::Duration::from_secs(60),
            32 * 1024 * 1024,
        )?
    };
    if !output.status.success() {
        return Err(format!(
            "GitHub request to {url} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        )
        .into());
    }
    Ok(String::from_utf8(output.stdout)?)
}

fn sha<'a>(value: &'a Value, label: &str) -> Result<&'a str, Box<dyn Error>> {
    value
        .as_str()
        .filter(|s| s.len() == 40 && s.chars().all(|c| c.is_ascii_hexdigit()))
        .ok_or_else(|| format!("missing or invalid {label}").into())
}

fn changed_package(pr: &PullRequest, api: &str) -> Result<(String, String), Box<dyn Error>> {
    let mut files = Vec::new();
    for page_number in 1..=100 {
        let body = get(&format!(
            "{api}/repos/{}/{}/pulls/{}/files?per_page=100&page={page_number}",
            pr.owner, pr.repo, pr.number
        ))?;
        let response: Value = serde_json::from_str(&body)?;
        let rows = response
            .as_array()
            .ok_or("GitHub PR files response is not an array")?;
        if rows.is_empty() {
            break;
        }
        files.extend(
            rows.iter()
                .filter_map(|row| row["filename"].as_str().map(str::to_owned)),
        );
        if rows.len() < 100 {
            break;
        }
        if page_number == 100 {
            return Err("PR changes too many files to inspect safely".into());
        }
    }
    if files.is_empty() {
        return Err("PR contains no changed files".into());
    }

    let tree_body = get(&format!(
        "{api}/repos/{}/{}/git/trees/{}?recursive=1",
        pr.owner, pr.repo, pr.head_sha
    ))?;
    let tree: Value = serde_json::from_str(&tree_body)?;
    if tree["truncated"].as_bool() == Some(true) {
        return Err(
            "repository tree is too large to identify changed Cargo packages safely".into(),
        );
    }
    let manifests: Vec<String> = tree["tree"]
        .as_array()
        .ok_or("missing GitHub repository tree")?
        .iter()
        .filter_map(|row| row["path"].as_str())
        .filter(|path| {
            ["Cargo.toml", "pyproject.toml", "setup.cfg", "setup.py"]
                .iter()
                .any(|name| path.rsplit('/').next() == Some(*name))
        })
        .map(str::to_owned)
        .collect();
    let mut package_manifests = BTreeMap::<(String, String), String>::new();
    for file in files {
        let mut directory = file.rsplit_once('/').map(|v| v.0.to_owned());
        if ["Cargo.toml", "pyproject.toml", "setup.cfg", "setup.py"].contains(&file.as_str()) {
            directory = Some(String::new());
        }
        while let Some(dir) = directory {
            let found = [
                ("rust", "Cargo.toml"),
                ("python", "pyproject.toml"),
                ("python", "setup.cfg"),
                ("python", "setup.py"),
            ]
            .iter()
            .find_map(|(ecosystem, filename)| {
                let path = if dir.is_empty() {
                    (*filename).to_owned()
                } else {
                    format!("{dir}/{filename}")
                };
                manifests
                    .iter()
                    .any(|manifest| manifest == &path)
                    .then(|| ((*ecosystem).to_owned(), dir.clone(), path))
            });
            if let Some((ecosystem, package_dir, manifest)) = found {
                package_manifests.insert((ecosystem, package_dir), manifest);
                break;
            }
            directory = if dir.is_empty() {
                None
            } else {
                Some(
                    dir.rsplit_once('/')
                        .map(|v| v.0.to_owned())
                        .unwrap_or_default(),
                )
            };
        }
    }
    if package_manifests.len() != 1 {
        return Err(format!(
            "expected changes in one Rust or Python package; found {}. Split multi-package PRs before checking",
            package_manifests.len()
        )
        .into());
    }
    let ((ecosystem, _), manifest_path) = package_manifests.iter().next().unwrap();
    let encoded_manifest_path = manifest_path
        .split('/')
        .map(url_encode)
        .collect::<Vec<_>>()
        .join("/");
    let body = get_raw(&format!(
        "{api}/repos/{}/{}/contents/{encoded_manifest_path}?ref={}",
        pr.owner, pr.repo, pr.head_sha
    ))?;
    let package_name = if ecosystem == "rust" {
        parse_package_name(&body)
    } else {
        parse_python_package_name(manifest_path, &body)
    }
    .ok_or_else(|| format!("could not read a static package name from {manifest_path}"))?;
    Ok((ecosystem.clone(), package_name))
}

fn parse_package_name(manifest: &str) -> Option<String> {
    let mut package = false;
    for line in manifest.lines() {
        let line = line.split('#').next().unwrap_or("").trim();
        if line.starts_with('[') && line.ends_with(']') {
            package = line == "[package]";
            continue;
        }
        if package {
            if let Some((key, value)) = line.split_once('=') {
                if key.trim() != "name" {
                    continue;
                }
                return value.trim().trim_matches(['"', '\'']).to_owned().into();
            }
        }
    }
    None
}

fn parse_python_package_name(path: &str, manifest: &str) -> Option<String> {
    if path.ends_with("pyproject.toml") {
        let mut section = "";
        for line in manifest.lines() {
            let line = line.split('#').next()?.trim();
            if line.starts_with('[') && line.ends_with(']') {
                section = line.trim_matches(['[', ']']);
            } else if matches!(section, "project" | "tool.poetry") {
                if let Some((key, value)) = line.split_once('=') {
                    if key.trim() == "name" {
                        return clean_quoted_value(value);
                    }
                }
            }
        }
    } else if path.ends_with("setup.cfg") {
        let mut metadata = false;
        for line in manifest.lines() {
            let line = line.split('#').next()?.trim();
            if line.starts_with('[') && line.ends_with(']') {
                metadata = line.eq_ignore_ascii_case("[metadata]");
            } else if metadata {
                if let Some((key, value)) = line.split_once('=') {
                    if key.trim().eq_ignore_ascii_case("name") {
                        return clean_manifest_value(value);
                    }
                }
            }
        }
    } else if path.ends_with("setup.py") {
        for line in manifest.lines() {
            let line = line.trim().strip_prefix("setup(").unwrap_or(line.trim());
            if let Some((key, value)) = line.split_once('=') {
                if key.trim().trim_matches(['(', ',', ' ']) == "name" {
                    return clean_quoted_value(value.trim().trim_end_matches([',', ')']).trim());
                }
            }
        }
    }
    None
}

fn clean_manifest_value(value: &str) -> Option<String> {
    let value = value
        .trim()
        .trim_end_matches(',')
        .trim()
        .trim_matches(['"', '\'']);
    (!value.is_empty() && !value.starts_with("{") && !value.starts_with("[")).then(|| value.into())
}

fn clean_quoted_value(value: &str) -> Option<String> {
    let value = value.trim();
    if !(value.starts_with('"') && value.ends_with('"')
        || value.starts_with('\'') && value.ends_with('\''))
    {
        return None;
    }
    clean_manifest_value(value)
}

fn validate_package_name(ecosystem: &str, name: &str) -> Result<(), Box<dyn Error>> {
    let valid_chars = name.chars().all(|c| {
        c.is_ascii_alphanumeric() || c == '-' || c == '_' || (ecosystem == "python" && c == '.')
    });
    if name.is_empty() || !valid_chars {
        return Err(format!("changed {ecosystem} package has an invalid package name").into());
    }
    Ok(())
}

fn latest_revision(
    repository: &str,
    priority: u64,
    host: &str,
) -> Result<Dependent, Box<dyn Error>> {
    let (owner, name) = github_repo(repository, host)?;
    let api = api_root(host);
    let repo_body = get(&format!("{api}/repos/{owner}/{name}"))?;
    let repo_json: Value = serde_json::from_str(&repo_body)?;
    let branch = repo_json["default_branch"]
        .as_str()
        .ok_or("missing downstream default branch")?;
    let branch = url_encode(branch);
    let commit_body = get(&format!(
        "{api}/repos/{owner}/{name}/commits?sha={branch}&per_page=1"
    ))?;
    let commit_json: Value = serde_json::from_str(&commit_body)?;
    let revision = sha(&commit_json[0]["sha"], "downstream head SHA")?.to_owned();
    Ok(Dependent {
        repository: format!("{owner}/{name}"),
        clone_url: format!("https://{host}/{owner}/{name}.git"),
        revision,
        priority,
    })
}

fn github_repo(repository: &str, expected_host: &str) -> Result<(String, String), Box<dyn Error>> {
    let rest = repository
        .strip_prefix("https://")
        .ok_or("dependent repository must use HTTPS")?;
    let (host, path) = rest
        .split_once('/')
        .ok_or("invalid dependent repository URL")?;
    if host != expected_host {
        return Err(format!("dependent repository is not hosted on {expected_host}").into());
    }
    let mut parts = path
        .trim_end_matches('/')
        .trim_end_matches(".git")
        .split('/');
    let owner = parts.next().ok_or("missing GitHub owner")?;
    let name = parts.next().ok_or("missing GitHub repository")?;
    let valid = |s: &str| {
        !s.is_empty()
            && s.chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
    };
    if !valid(owner) || !valid(name) || parts.next().is_some() {
        return Err("invalid GitHub dependent repository URL".into());
    }
    Ok((owner.to_owned(), name.to_owned()))
}

fn discover_company_repositories(
    crate_name: &str,
    limit: usize,
    lookup_budget: usize,
) -> Result<RepositoryDiscovery, Box<dyn Error>> {
    let Ok(org) = env::var("DETECTOR_GITHUB_ORG") else {
        return Ok((Vec::new(), Vec::new(), 0));
    };
    if org.is_empty()
        || !org
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
    {
        return Err("DETECTOR_GITHUB_ORG must be a GitHub organization or user name".into());
    }
    let host = env::var("DETECTOR_GITHUB_HOST").unwrap_or_else(|_| "github.com".into());
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '-'))
    {
        return Err("invalid DETECTOR_GITHUB_HOST".into());
    }
    let priority_file = env::var("DETECTOR_PRIORITY_FILE").ok();
    let priorities: BTreeMap<String, u64> = if let Some(path) = priority_file {
        serde_json::from_str(&std::fs::read_to_string(path)?)?
    } else {
        BTreeMap::new()
    };
    let api = api_root(&host);
    let mut repos = BTreeMap::<String, u64>::new();
    for page in 1..=10 {
        let url = format!(
            "{api}/search/code?q={crate_name}+filename%3ACargo.toml+org%3A{org}&per_page=100&page={page}"
        );
        let response: Value = serde_json::from_str(&get(&url)?)?;
        if response["total_count"]
            .as_u64()
            .is_some_and(|count| count > 1000)
        {
            return Err("GitHub code search exceeds its 1,000-result cap; refusing an incomplete company scan".into());
        }
        let items = response["items"]
            .as_array()
            .ok_or("GitHub code search response is missing items")?;
        if page == 10 && items.len() == 100 {
            return Err("GitHub code search reached its 1,000-result page cap; refusing an incomplete company scan".into());
        }
        for item in items {
            if let Some(full_name) = item["repository"]["full_name"].as_str() {
                let priority = priorities.get(full_name).copied().unwrap_or(0);
                repos
                    .entry(full_name.to_owned())
                    .and_modify(|p| *p = (*p).max(priority))
                    .or_insert(priority);
            }
        }
        if items.len() < 100 {
            break;
        }
    }
    let mut selected: Vec<_> = repos.into_iter().collect();
    selected.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let mut ready = Vec::new();
    let mut skipped = Vec::new();
    let mut attempts = 0;
    for (repository, priority) in selected {
        if ready.len() >= limit || attempts >= lookup_budget {
            break;
        }
        attempts += 1;
        match latest_revision(&format!("https://{host}/{repository}"), priority, &host) {
            Ok(dep) => ready.push(dep),
            Err(e) => skipped.push(json!({"repository":repository,"reason":e.to_string()})),
        }
    }
    Ok((ready, skipped, attempts))
}

fn discover_python_repositories(
    package: &str,
    limit: usize,
    lookup_budget: usize,
) -> Result<RepositoryDiscovery, Box<dyn Error>> {
    let host = env::var("DETECTOR_GITHUB_HOST").unwrap_or_else(|_| "github.com".into());
    let org = env::var("DETECTOR_GITHUB_ORG").ok();
    if let Some(org) = &org {
        if org.is_empty()
            || !org
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_'))
        {
            return Err("DETECTOR_GITHUB_ORG must be a GitHub organization or user name".into());
        }
    }
    if host.is_empty()
        || !host
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '-'))
    {
        return Err("invalid DETECTOR_GITHUB_HOST".into());
    }
    let api_host = if host == "github.com" {
        "api.github.com"
    } else {
        host.as_str()
    };
    let has_token = match token_env_for_api_host(api_host, Some(&host)) {
        Some("GITHUB_TOKEN") => env::var("GITHUB_TOKEN")
            .or_else(|_| env::var("GH_TOKEN"))
            .is_ok(),
        Some("DETECTOR_GHES_TOKEN") => env::var("DETECTOR_GHES_TOKEN").is_ok(),
        _ => false,
    };
    if !has_token {
        return Err("Python consumer discovery uses GitHub code search; set a read-only GitHub API token (GITHUB_TOKEN or DETECTOR_GHES_TOKEN)".into());
    }
    let priorities: BTreeMap<String, u64> = match env::var("DETECTOR_PRIORITY_FILE") {
        Ok(path) => serde_json::from_str(&std::fs::read_to_string(path)?)?,
        Err(_) => BTreeMap::new(),
    };
    let api = api_root(&host);
    let mut repos = BTreeMap::<String, u64>::new();
    let mut skipped = vec![
        json!({"source":"GitHub code search","reason":"Python discovery is a best-effort sample, limited to up to 100 code matches per manifest; it cannot guarantee full ecosystem coverage"}),
    ];
    let mut scopes = vec![None];
    if let Some(org) = org.as_deref() {
        scopes.push(Some(org));
    }
    for scope in scopes {
        for filename in [
            "pyproject.toml",
            "requirements.txt",
            "setup.cfg",
            "setup.py",
        ] {
            let query = match scope {
                Some(org) => format!(
                    "{}+filename%3A{}+org%3A{}",
                    url_encode(&normalize_python_package_name(package)),
                    filename,
                    url_encode(org)
                ),
                None => format!(
                    "{}+filename%3A{}",
                    url_encode(&normalize_python_package_name(package)),
                    filename
                ),
            };
            let url = format!("{api}/search/code?q={query}&per_page=100&page=1");
            let response: Value = serde_json::from_str(&get(&url)?)?;
            let items = response["items"]
                .as_array()
                .ok_or("GitHub code search response is missing items")?;
            for (index, item) in items.iter().enumerate() {
                if let Some(full_name) = item["repository"]["full_name"].as_str() {
                    let relevance = 10000u64.saturating_sub(index as u64);
                    let priority = priorities
                        .get(full_name)
                        .map(|value| 1_000_000u64.saturating_add(*value))
                        .unwrap_or(relevance);
                    repos
                        .entry(full_name.to_owned())
                        .and_modify(|existing| *existing = (*existing).max(priority))
                        .or_insert(priority);
                }
            }
        }
    }
    let discovered = repos.len();
    let mut selected: Vec<_> = repos.into_iter().collect();
    selected.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    let mut ready = Vec::new();
    for (attempts, (repository, priority)) in selected.into_iter().enumerate() {
        if ready.len() >= limit || attempts >= lookup_budget {
            break;
        }
        match latest_revision(&format!("https://{host}/{repository}"), priority, &host) {
            Ok(dependent) => ready.push(dependent),
            Err(error) => skipped.push(json!({"repository":repository,"reason":error.to_string()})),
        }
    }
    Ok((ready, skipped, discovered))
}

fn normalize_python_package_name(name: &str) -> String {
    let mut normalized = String::new();
    let mut separator = false;
    for character in name.chars() {
        if matches!(character, '-' | '_' | '.') {
            separator = true;
        } else {
            if separator && !normalized.is_empty() {
                normalized.push('-');
            }
            separator = false;
            normalized.push(character.to_ascii_lowercase());
        }
    }
    normalized
}

fn url_encode(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::{
        api_root, approved_github_host, github_repo, normalize_python_package_name,
        parse_package_name, parse_pr_url, parse_python_package_name, token_env_for_api_host,
        validate_package_name,
    };

    #[test]
    fn parses_public_and_enterprise_pr_urls() {
        assert_eq!(
            parse_pr_url("https://github.com/acme/lib/pull/42").unwrap(),
            ("github.com".into(), "acme".into(), "lib".into(), 42)
        );
        assert_eq!(
            parse_pr_url("https://git.example.com/acme/lib/pull/7?diff=split")
                .unwrap()
                .3,
            7
        );
        assert_eq!(
            api_root("git.example.com"),
            "https://git.example.com/api/v3"
        );
        assert!(parse_pr_url("http://github.com/acme/lib/pull/42").is_err());
        assert!(approved_github_host("github.com", None));
        assert!(approved_github_host(
            "git.example.com",
            Some("git.example.com")
        ));
        assert!(!approved_github_host(
            "evil.example",
            Some("git.example.com")
        ));
        assert_eq!(
            token_env_for_api_host("api.github.com", Some("git.example.com")),
            Some("GITHUB_TOKEN")
        );
        assert_eq!(
            token_env_for_api_host("git.example.com", Some("git.example.com")),
            Some("DETECTOR_GHES_TOKEN")
        );
        assert_eq!(
            token_env_for_api_host("evil.example", Some("git.example.com")),
            None
        );
    }

    #[test]
    fn finds_package_name_only_in_package_section() {
        assert_eq!(
            parse_package_name(
                "[workspace]\nmembers=[]\n[package]\nname = \"my-crate\"\nversion=\"1\""
            ),
            Some("my-crate".into())
        );
        assert_eq!(
            parse_package_name("[workspace]\nname = \"not-a-package\""),
            None
        );
    }

    #[test]
    fn parses_common_static_python_package_names() {
        assert_eq!(
            normalize_python_package_name("My.Package_Name"),
            "my-package-name"
        );
        assert_eq!(
            parse_python_package_name("pyproject.toml", "[project]\nname = 'fast-api'\n"),
            Some("fast-api".into())
        );
        assert_eq!(
            parse_python_package_name("pyproject.toml", "[tool.poetry]\nname = \"my_pkg\"\n"),
            Some("my_pkg".into())
        );
        assert_eq!(
            parse_python_package_name("setup.cfg", "[metadata]\nname = sample.pkg\n"),
            Some("sample.pkg".into())
        );
        assert_eq!(
            parse_python_package_name("setup.py", "setup(\n    name='legacy-pkg',\n)"),
            Some("legacy-pkg".into())
        );
        assert_eq!(
            parse_python_package_name("setup.py", "setup(name='one-line-pkg')"),
            Some("one-line-pkg".into())
        );
        assert!(
            parse_python_package_name("pyproject.toml", "[project]\nname = dynamic_name\n")
                .is_none()
        );
        assert!(validate_package_name("python", "Sample.pkg_1").is_ok());
        assert!(validate_package_name("python", "../evil").is_err());
    }

    #[test]
    fn accepts_only_simple_github_repository_urls() {
        assert_eq!(
            github_repo("https://github.com/acme/lib.git", "github.com").unwrap(),
            ("acme".into(), "lib".into())
        );
        assert!(github_repo("https://evil.example/acme/lib", "github.com").is_err());
        assert!(github_repo("https://github.com/acme/lib@evil", "github.com").is_err());
    }
}
