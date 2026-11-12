use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    env,
    error::Error,
    io::Write,
    process::{Command, Stdio},
};

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
    downloads: u64,
}

pub fn check(pr_url: &str) -> Result<String, Box<dyn Error>> {
    let (host, owner, repo, number) = parse_pr_url(pr_url)?;
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

    let crate_name = changed_crate(&pr, &api)?;
    let user_agent = env::var("CRATES_IO_USER_AGENT").map_err(|_| {
        "set CRATES_IO_USER_AGENT once with an identifying name and contact before using check"
    })?;
    let discovery: Value = match crate::discovery::discover(&crate_name, &user_agent) {
        Ok(body) => serde_json::from_str(&body)?,
        Err(error) if env::var("DETECTOR_GITHUB_ORG").is_ok() => {
            eprintln!("crates.io discovery unavailable ({error}); checking configured company repositories");
            json!({"upstream":crate_name,"dependents":[]})
        }
        Err(error) => return Err(error),
    };
    let max_jobs = env::var("DETECTOR_MAX_JOBS")
        .unwrap_or_else(|_| "5".into())
        .parse::<usize>()?;
    if max_jobs == 0 || max_jobs > 32 {
        return Err("DETECTOR_MAX_JOBS must be between 1 and 32".into());
    }
    let plan: Value =
        serde_json::from_str(&crate::plan::make_plan(&discovery.to_string(), max_jobs)?)?;
    let mut dependents = Vec::new();
    let (company_repositories, mut skipped) = discover_company_repositories(&crate_name, max_jobs)?;
    for dep in company_repositories {
        dependents.push(dep);
        if dependents.len() >= max_jobs {
            break;
        }
    }
    for item in plan["selected"]
        .as_array()
        .ok_or("missing selected dependents")?
    {
        if dependents.len() >= max_jobs {
            break;
        }
        let Some(repository) = item["repository"].as_str() else {
            continue;
        };
        let downloads = item["downloads"].as_u64().unwrap_or(0);
        match latest_revision(repository, downloads, "github.com") {
            Ok(dep) => dependents.push(dep),
            Err(e) => skipped.push(json!({"repository":repository,"reason":e.to_string()})),
        }
    }
    if dependents.is_empty() {
        return Err(
            format!("no runnable public downstream repositories found for {crate_name}").into(),
        );
    }
    let planned_jobs: Vec<Value> = dependents
        .iter()
        .map(|d| {
            json!({
                "repository":d.repository,
                "clone_url":d.clone_url,
                "revision":d.revision,
                "download_priority":d.downloads,
            })
        })
        .collect();
    if env::var("DETECTOR_DRY_RUN").is_ok_and(|value| value == "true") {
        return Ok(serde_json::to_string_pretty(&json!({
            "pull_request":pr_url,
            "crate":crate_name,
            "baseline":pr.base_sha,
            "candidate":pr.head_sha,
            "discovered_repositories":plan["eligible_repositories"],
            "selected_repositories":dependents.len(),
            "skipped_repositories":skipped,
            "planned_jobs":planned_jobs,
        }))?);
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
        "crate_name":crate_name,
        "max_jobs":max_jobs,
        "max_parallel":parallel,
        "cargo_offline":false,
        "jobs":dependents.iter().map(|d|json!({"repository":d.clone_url,"revision":d.revision,"priority":d.downloads})).collect::<Vec<_>>(),
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
            config["egress_policy"] = json!(env::var("DETECTOR_EGRESS_POLICY")
                .unwrap_or_else(|_| "substrate/egress-policy.yaml".into()));
            if let Ok(auth_host) = env::var("DETECTOR_GIT_AUTH_HOST") {
                config["git_auth_host"] = json!(auth_host);
            } else if let Ok(company_host) = env::var("DETECTOR_GITHUB_HOST") {
                config["git_auth_host"] = json!(company_host);
            } else if pr.host != "github.com" {
                config["git_auth_host"] = json!(pr.host);
            }
        }
        "command" => {
            let command = env::var("DETECTOR_EXECUTOR_COMMAND")
                .map_err(|_| "set DETECTOR_EXECUTOR_COMMAND to your trusted executor adapter")?;
            config["executor"] = json!({"type":"command","command":command});
        }
        _ => return Err("DETECTOR_EXECUTOR must be 'substrate' or 'command'".into()),
    }
    let results: Value = serde_json::from_str(&crate::batch::run(&config.to_string())?)?;
    let full_report = json!({
        "pull_request":pr_url,
        "crate":crate_name,
        "baseline":pr.base_sha,
        "candidate":pr.head_sha,
        "discovered_repositories":plan["eligible_repositories"],
        "selected_repositories":dependents.len(),
        "skipped_repositories":skipped,
        "results":results,
    });
    let report_dir =
        env::var("DETECTOR_REPORT_DIR").unwrap_or_else(|_| ".downstream-detector/reports".into());
    std::fs::create_dir_all(&report_dir)?;
    let report_path = std::path::Path::new(&report_dir)
        .join(format!("{}-{}-pr-{}.json", pr.owner, pr.repo, pr.number));
    std::fs::write(&report_path, serde_json::to_vec_pretty(&full_report)?)?;

    let runs = full_report["results"]["results"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let mut summary = format!(
        "PR check: {pr_url}\nCrate: {crate_name}\nDownstream repositories tested: {}\n",
        runs.len()
    );
    for run in runs {
        let repository = run["repository"].as_str().unwrap_or("unknown repository");
        let result = &run["result"];
        let outcome = if result["status"] == "completed" {
            result["report"]
                .as_str()
                .and_then(|report| report.lines().find(|line| line.starts_with("Verdict:")))
                .unwrap_or("Completed; see full log")
                .to_owned()
        } else {
            format!(
                "ERROR: {}",
                result["error"].as_str().unwrap_or("unknown error")
            )
        };
        summary.push_str(&format!("  {repository}: {outcome}\n"));
    }
    summary.push_str(&format!("Full logs: {}", report_path.display()));
    Ok(summary)
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
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-time",
        "60",
        "--retry",
        "2",
        "--retry-connrefused",
        "--retry-delay",
        "1",
        "--header",
        &format!("Accept: {accept}"),
    ]);
    command.arg(url);
    let token = env::var("GITHUB_TOKEN").or_else(|_| env::var("GH_TOKEN"));
    let output = if let Ok(token) = token {
        if token.trim().is_empty()
            || !token
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        {
            return Err("GITHUB_TOKEN or GH_TOKEN contains invalid characters".into());
        }
        command
            .args(["--config", "-"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn()?;
        child
            .stdin
            .take()
            .ok_or("could not open curl stdin")?
            .write_all(format!("header = \"Authorization: Bearer {token}\"\n").as_bytes())?;
        child.wait_with_output()?
    } else {
        command.output()?
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

fn changed_crate(pr: &PullRequest, api: &str) -> Result<String, Box<dyn Error>> {
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
        .filter(|path| path.ends_with("Cargo.toml"))
        .map(str::to_owned)
        .collect();
    let mut package_manifests = BTreeMap::new();
    for file in files {
        let mut directory = file.rsplit_once('/').map(|v| v.0.to_owned());
        if file == "Cargo.toml" {
            directory = Some(String::new());
        }
        while let Some(dir) = directory {
            let path = if dir.is_empty() {
                "Cargo.toml".to_owned()
            } else {
                format!("{dir}/Cargo.toml")
            };
            if manifests.iter().any(|p| p == &path) {
                package_manifests.insert(path.clone(), ());
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
            "expected changes in one Rust package; found {}. Split multi-crate PRs before checking",
            package_manifests.len()
        )
        .into());
    }
    let manifest_path = package_manifests.keys().next().unwrap();
    let body = get_raw(&format!(
        "{api}/repos/{}/{}/contents/{manifest_path}?ref={}",
        pr.owner, pr.repo, pr.head_sha
    ))?;
    let crate_name =
        parse_package_name(&body).ok_or("could not read [package].name from changed Cargo.toml")?;
    Ok(crate_name)
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

fn latest_revision(
    repository: &str,
    downloads: u64,
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
        downloads,
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
) -> Result<(Vec<Dependent>, Vec<Value>), Box<dyn Error>> {
    let Ok(org) = env::var("DETECTOR_GITHUB_ORG") else {
        return Ok((Vec::new(), Vec::new()));
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
        let items = response["items"]
            .as_array()
            .ok_or("GitHub code search response is missing items")?;
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
    for (repository, priority) in selected.into_iter().take(limit) {
        match latest_revision(&format!("https://{host}/{repository}"), priority, &host) {
            Ok(dep) => ready.push(dep),
            Err(e) => skipped.push(json!({"repository":repository,"reason":e.to_string()})),
        }
    }
    Ok((ready, skipped))
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
    use super::{api_root, github_repo, parse_package_name, parse_pr_url};

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
    fn accepts_only_simple_github_repository_urls() {
        assert_eq!(
            github_repo("https://github.com/acme/lib.git", "github.com").unwrap(),
            ("acme".into(), "lib".into())
        );
        assert!(github_repo("https://evil.example/acme/lib", "github.com").is_err());
        assert!(github_repo("https://github.com/acme/lib@evil", "github.com").is_err());
    }
}
