use serde_json::{json, Value};
use std::{
    error::Error,
    fs,
    io::{Read, Write},
    net::TcpListener,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

// The controller validates and schedules jobs. Execution details belong to
// the configured backend; untrusted code never receives controller access.
pub fn run(input: &str) -> Result<String, Box<dyn Error>> {
    let config: Value = serde_json::from_str(input)?;
    let get = |key: &str| {
        config[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("missing {key}"))
    };
    let executor = config["executor"]["type"].as_str().unwrap_or("substrate");
    match executor {
        "substrate" => {
            get("atespace")?;
            let image = get("image")?;
            if !image.contains("@sha256:") {
                return Err("image must be digest-pinned".into());
            }
        }
        "command" => {
            config["executor"]["command"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or("executor.command is required for command executor")?;
        }
        _ => return Err("executor.type must be 'substrate' or 'command'".into()),
    }
    for key in ["upstream", "baseline", "candidate", "crate_name"] {
        get(key)?;
    }
    let ecosystem = config["ecosystem"].as_str().unwrap_or("rust");
    if !matches!(ecosystem, "rust" | "python") {
        return Err("ecosystem must be 'rust' or 'python'".into());
    }
    if has_url_userinfo(get("upstream")?) {
        return Err("upstream URLs must not contain credentials".into());
    }
    let allowed_git_host = config["git_auth_host"].as_str();
    if let Some(host) = allowed_git_host {
        if host.is_empty()
            || !host
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '-'))
        {
            return Err("git_auth_host must be a valid hostname".into());
        }
    }
    let check_git_host = |url: &str| -> Result<(), Box<dyn Error>> {
        let host = https_host(url).ok_or("repository URL must use HTTPS and have a host")?;
        if host != "github.com" && Some(host) != allowed_git_host {
            return Err(format!("repository host '{host}' is not approved for this run").into());
        }
        Ok(())
    };
    check_git_host(get("upstream")?)?;
    let max_jobs = config["max_jobs"].as_u64().ok_or("max_jobs is required")? as usize;
    if max_jobs == 0 || max_jobs > 32 {
        return Err("max_jobs must be between 1 and 32".into());
    }
    let parallel = config["max_parallel"]
        .as_u64()
        .ok_or("max_parallel is required")? as usize;
    if parallel == 0 || parallel > 32 {
        return Err("max_parallel must be between 1 and 32".into());
    }
    if let Some(candidate_sha) = config["candidate_sha"].as_str() {
        if candidate_sha.len() != 40 || !candidate_sha.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err("candidate_sha must be a full commit SHA".into());
        }
    }
    let mut jobs = config["jobs"]
        .as_array()
        .ok_or("jobs array is required")?
        .clone();
    for job in &jobs {
        if job["repository"]
            .as_str()
            .filter(|s| !s.is_empty())
            .is_none()
            || job["revision"].as_str().filter(|s| !s.is_empty()).is_none()
        {
            return Err("every job needs a repository and revision".into());
        }
        if has_url_userinfo(job["repository"].as_str().unwrap()) {
            return Err("repository URLs must not contain credentials".into());
        }
        check_git_host(job["repository"].as_str().unwrap())?;
        if job["priority"].as_i64().is_none() {
            return Err("every job needs an integer priority; higher runs first".into());
        }
    }
    jobs.sort_by(|a, b| {
        b["priority"]
            .as_i64()
            .unwrap_or(0)
            .cmp(&a["priority"].as_i64().unwrap_or(0))
    });
    let omitted = jobs.len().saturating_sub(max_jobs);
    jobs.truncate(max_jobs);
    let run_id = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let next = AtomicUsize::new(0);
    let results = Mutex::new(Vec::new());
    thread::scope(|scope| {
        for _ in 0..parallel.min(jobs.len()) {
            let (jobs, config, results, next) = (&jobs, &config, &results, &next);
            scope.spawn(move || loop {
                let index = next.fetch_add(1, Ordering::Relaxed);
                if index >= jobs.len() { break; }
                let job = &jobs[index];
                let outcome = run_job(config, job, run_id, index).unwrap_or_else(|e| json!({"status":"error", "error":e}));
                results.lock().unwrap().push((index, json!({"repository":job["repository"], "revision":job["revision"], "priority":job["priority"], "result":outcome})));
            });
        }
    });
    let mut results = results.into_inner().unwrap();
    results.sort_by_key(|v| v.0);
    Ok(serde_json::to_string_pretty(
        &json!({"executor":executor,"atespace":config["atespace"],"selected":jobs.len(),"omitted_by_budget":omitted,"max_parallel":parallel,"results":results.into_iter().map(|v|v.1).collect::<Vec<_>>()}),
    )?)
}

fn has_url_userinfo(s: &str) -> bool {
    s.split_once("://")
        .map(|(_, rest)| rest)
        .and_then(|tail| tail.split('/').next())
        .is_some_and(|authority| authority.contains('@'))
}

fn create_run_token() -> Result<String, String> {
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut bytes))
        .map_err(|e| format!("cannot generate actor request token: {e}"))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn https_host(url: &str) -> Option<&str> {
    url.strip_prefix("https://")?
        .split_once('/')
        .map(|(host, _)| host)
        .filter(|host| {
            !host.is_empty()
                && host
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '-'))
        })
}

fn kubectl(args: &[&str]) -> Result<String, String> {
    let mut command = Command::new("kubectl");
    command.arg("ate").args(args);
    let output = crate::process::run_limited(&mut command, Duration::from_secs(60), 1024 * 1024)
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "{}{}",
            String::from_utf8_lossy(&output.stderr),
            String::from_utf8_lossy(&output.stdout)
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn kubectl_delete(args: &[&str]) -> Result<(), String> {
    match kubectl(args) {
        Ok(_) => Ok(()),
        Err(error)
            if error.to_ascii_lowercase().contains("not found")
                || error.to_ascii_lowercase().contains("does not exist") =>
        {
            Ok(())
        }
        Err(error) => Err(error),
    }
}

struct Proxy(Child);
impl Drop for Proxy {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn run_job(c: &Value, job: &Value, run_id: u128, index: usize) -> Result<Value, String> {
    if c["executor"]["type"] == "command" {
        return run_command_executor(c, job);
    }
    let space = c["atespace"].as_str().unwrap();
    let name = format!("compat-{}-{index}", run_id);
    let mut template = json!({"metadata":{"atespace":space,"name":name},
        "workerSelector":{"matchLabels":{"workload":"rust-detector"}},
        "containers":[{"name":"detector", "image":c["image"], "args":["serve",c["upstream"],c["baseline"],c["candidate"],c["crate_name"],job["repository"],job["revision"]],
            "wakeupProbe":{"httpGet":{"path":"/readyz","port":8080}}}],
        "snapshotConfig":{"goldenSnapshotConfig":{"mode":"GOLDEN_SNAPSHOT_MODE_DISABLED"}},
        "resources":{"limits":{"cpuMillicores":1000,"memoryBytes":1073741824u64}}});
    let mut actor_env = Vec::new();
    let run_token = create_run_token()?;
    actor_env.push(json!({"name":"DETECTOR_RUN_TOKEN", "value":run_token.clone()}));
    actor_env.push(
        json!({"name":"DETECTOR_ECOSYSTEM", "value":c["ecosystem"].as_str().unwrap_or("rust")}),
    );
    if c["cargo_offline"] == false {
        actor_env.push(json!({"name":"DETECTOR_CARGO_OFFLINE", "value":"false"}));
    }
    if let Some(candidate_sha) = c["candidate_sha"].as_str() {
        actor_env.push(json!({"name":"DETECTOR_EXPECT_CANDIDATE_SHA", "value":candidate_sha}));
    }
    if let Some(source) = c["patch_source"].as_str() {
        template["containers"][0]["args"]
            .as_array_mut()
            .unwrap()
            .push(json!(source));
    }
    if let Some(host) = c["git_auth_host"].as_str() {
        if host.is_empty()
            || !host
                .chars()
                .all(|x| x.is_ascii_lowercase() || x.is_ascii_digit() || x == '.' || x == '-')
        {
            return Err("invalid git_auth_host".into());
        }
        if c["egress_policy"].as_str().is_none() {
            return Err("git_auth_host requires an egress_policy".into());
        }
        actor_env.push(json!({"name":"DETECTOR_GIT_AUTH_HOST", "value":host}));
        actor_env.push(json!({"name":"GIT_SSL_CAINFO", "value":"/run/ate/egress-trust-bundle/trust-bundle.pem"}));
        template["containers"][0]["volumeMounts"] =
            json!([{"name":"system-info","mountPath":"/run/ate/egress-trust-bundle"}]);
        template["volumes"] = json!([{"name":"system-info","systemInfo":{"dataSources":[{"trustBundle":{"names":["egress-mitm.ate.dev","system-roots.ate.dev"],"path":"trust-bundle.pem"}}]}}]);
    }
    if !actor_env.is_empty() {
        template["containers"][0]["env"] = json!(actor_env);
    }
    let path = std::env::temp_dir().join(format!("{name}.json"));
    let mut file_options = fs::OpenOptions::new();
    file_options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        file_options.mode(0o600);
    }
    let mut template_file = file_options.open(&path).map_err(|e| e.to_string())?;
    if let Err(error) = template_file.write_all(template.to_string().as_bytes()) {
        drop(template_file);
        let _ = fs::remove_file(&path);
        return Err(error.to_string());
    }
    let path_str = path.to_str().ok_or("invalid temporary path")?;
    let result = kubectl(&["create", "actor-template", "-f", path_str]);
    let _ = fs::remove_file(&path);
    if let Err(error) = result {
        let _ = kubectl_delete(&["delete", "actor-template", &name, "-a", space]);
        return Err(error);
    }
    if let Err(error) = kubectl(&["create", "actor", &name, "-a", space, "--template", &name]) {
        let _ = kubectl_delete(&["delete", "actor-template", &name, "-a", space]);
        return Err(error);
    }
    let mut egress_attempted = false;
    let result = (|| {
        if let Some(policy) = c["egress_policy"].as_str() {
            egress_attempted = true;
            kubectl(&["create", "egress-policy", &name, "-a", space, "-f", policy])?;
        }
        kubectl(&["resume", "actor", &name, "-a", space])?;
        let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        drop(listener);
        let mapping = format!("{port}:8080");
        let proxy = Command::new("kubectl")
            .args(["ate", "proxy", "actor", &name, &mapping, "-a", space])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        let _proxy = Proxy(proxy);
        let url = format!("http://127.0.0.1:{port}/run");
        wait_for_actor_ready(&format!("http://127.0.0.1:{port}/readyz"))?;
        let mut curl = Command::new("curl");
        curl.args([
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--max-time",
            "3660",
            "--max-filesize",
            "33554432",
            "--config",
            "-",
            "-X",
            "POST",
            &url,
        ]);
        let config = format!("header = \"Authorization: Bearer {run_token}\"\n");
        let output = crate::process::run_with_stdin(
            &mut curl,
            config.as_bytes(),
            Duration::from_secs(3660),
            32 * 1024 * 1024,
        )
        .map_err(|e| e.to_string())?;
        if !output.status.success() {
            return Err(format!(
                "actor request failed: {} {}",
                String::from_utf8_lossy(&output.stderr),
                String::from_utf8_lossy(&output.stdout)
            ));
        }
        Ok(
            json!({"status":"completed","actor":name,"report":String::from_utf8_lossy(&output.stdout)}),
        )
    })();
    let mut cleanup_errors = Vec::new();
    if let Err(error) = kubectl(&["suspend", "actor", &name, "-a", space, "--mode=sync"]) {
        cleanup_errors.push(format!("suspend: {error}"));
    }
    if egress_attempted {
        if let Err(error) = kubectl_delete(&["delete", "egress-policy", &name, "-a", space]) {
            cleanup_errors.push(format!("delete egress policy: {error}"));
        }
    }
    if let Err(error) = kubectl_delete(&["delete", "actor", &name, "-a", space]) {
        cleanup_errors.push(format!("delete actor: {error}"));
    }
    if let Err(error) = kubectl_delete(&["delete", "actor-template", &name, "-a", space]) {
        cleanup_errors.push(format!("delete actor template: {error}"));
    }
    match (result, cleanup_errors.is_empty()) {
        (Ok(value), true) => Ok(value),
        (Ok(_), false) => Err(format!(
            "comparison completed but cleanup failed: {}",
            cleanup_errors.join("; ")
        )),
        (Err(error), true) => Err(error),
        (Err(error), false) => Err(format!(
            "{error}; cleanup also failed: {}",
            cleanup_errors.join("; ")
        )),
    }
}

fn wait_for_actor_ready(url: &str) -> Result<(), String> {
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    loop {
        let mut request = Command::new("curl");
        request.args(["--fail", "--silent", "--show-error", "--max-time", "2", url]);
        if let Ok(response) =
            crate::process::run_limited(&mut request, Duration::from_secs(3), 1024)
        {
            if response.status.success() {
                return Ok(());
            }
        }
        if std::time::Instant::now() >= deadline {
            return Err(
                "actor readiness endpoint did not become available within 60 seconds".into(),
            );
        }
        thread::sleep(Duration::from_secs(1));
    }
}

// The command adapter is a transport boundary: the adapter owns provisioning,
// isolation, cancellation and log collection in its chosen execution platform.
// It receives one JSON request on stdin and returns one JSON result on stdout.
fn run_command_executor(c: &Value, job: &Value) -> Result<Value, String> {
    let command = c["executor"]["command"]
        .as_str()
        .ok_or("executor.command is required")?;
    if !std::path::Path::new(command).is_absolute() {
        return Err("executor.command must be an absolute path".into());
    }
    let request = json!({
        "protocol_version": 1,
        "operation": "compatibility_check",
        "ecosystem": c["ecosystem"].as_str().unwrap_or("rust"),
        "upstream": c["upstream"],
        "baseline": c["baseline"],
        "candidate": c["candidate"],
        "candidate_sha": c["candidate_sha"],
        "crate_name": c["crate_name"],
        "downstream": job["repository"],
        "downstream_ref": job["revision"],
        "git_auth_host": c["git_auth_host"],
        "patch_source": c["patch_source"],
        "cargo_offline": c["cargo_offline"],
        "resources": {"cpu_millicores": 1000, "memory_bytes": 1073741824u64},
    });
    let timeout = Duration::from_secs(
        std::env::var("DETECTOR_EXECUTOR_TIMEOUT_SECS")
            .unwrap_or_else(|_| "660".into())
            .parse::<u64>()
            .map_err(|e| format!("invalid DETECTOR_EXECUTOR_TIMEOUT_SECS: {e}"))?,
    );
    if timeout.is_zero() || timeout > Duration::from_secs(3660) {
        return Err("DETECTOR_EXECUTOR_TIMEOUT_SECS must be between 1 and 3660".into());
    }
    let mut command = Command::new(command);
    command
        .env_remove("GITHUB_TOKEN")
        .env_remove("GH_TOKEN")
        .env_remove("DETECTOR_GHES_TOKEN")
        .env_remove("DETECTOR_RUN_TOKEN");
    let output = crate::process::run_with_stdin(
        &mut command,
        request.to_string().as_bytes(),
        timeout,
        16 * 1024 * 1024,
    )
    .map_err(|e| format!("cannot collect executor result: {e}"))?;
    if String::from_utf8_lossy(&output.stderr).contains("[output truncated") {
        return Err("executor stderr exceeded the 16 MiB output cap".into());
    }
    if !output.status.success() {
        return Err(format!(
            "executor command failed ({}): {}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    let response: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("executor returned invalid JSON: {e}"))?;
    if response["status"] != "completed" {
        return Err(format!(
            "executor returned status {:?}: {}",
            response["status"], response["error"]
        ));
    }
    if c["ecosystem"] == "python" && response["ecosystem"] != "python" {
        return Err("Python executor response must echo ecosystem: 'python'".into());
    }
    let report = response["report"]
        .as_str()
        .ok_or("executor result must contain a string 'report'")?;
    Ok(json!({
        "status":"completed",
        "executor":response["executor"],
        "report":report,
    }))
}
