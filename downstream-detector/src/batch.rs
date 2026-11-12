use serde_json::{json, Value};
use std::{
    error::Error,
    fs,
    net::TcpListener,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    },
    thread,
    time::{SystemTime, UNIX_EPOCH},
};

// The controller runs outside actors and holds Kubernetes access. Each job
// gets a separate actor; untrusted code never receives Kubernetes credentials.
pub fn run(input: &str) -> Result<String, Box<dyn Error>> {
    let config: Value = serde_json::from_str(input)?;
    let get = |key: &str| {
        config[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("missing {key}"))
    };
    let space = get("atespace")?;
    let image = get("image")?;
    if !image.contains("@sha256:") {
        return Err("image must be digest-pinned".into());
    }
    for key in ["upstream", "baseline", "candidate", "crate_name"] {
        get(key)?;
    }
    if has_url_userinfo(get("upstream")?) {
        return Err("upstream URLs must not contain credentials".into());
    }
    let max_jobs = config["max_jobs"].as_u64().ok_or("max_jobs is required")? as usize;
    let parallel = config["max_parallel"]
        .as_u64()
        .ok_or("max_parallel is required")? as usize;
    if parallel == 0 || parallel > 32 {
        return Err("max_parallel must be between 1 and 32".into());
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
        &json!({"atespace":space,"selected":jobs.len(),"omitted_by_budget":omitted,"max_parallel":parallel,"results":results.into_iter().map(|v|v.1).collect::<Vec<_>>()}),
    )?)
}

fn has_url_userinfo(s: &str) -> bool {
    s.split_once("://")
        .map(|(_, rest)| rest)
        .and_then(|tail| tail.split('/').next())
        .is_some_and(|authority| authority.contains('@'))
}

fn kubectl(args: &[&str]) -> Result<String, String> {
    let output = Command::new("kubectl")
        .arg("ate")
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

struct Proxy(Child);
impl Drop for Proxy {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn run_job(c: &Value, job: &Value, run_id: u128, index: usize) -> Result<Value, String> {
    let space = c["atespace"].as_str().unwrap();
    let name = format!("compat-{}-{index}", run_id);
    let mut template = json!({"metadata":{"atespace":space,"name":name},
        "workerSelector":{"matchLabels":{"workload":"rust-detector"}},
        "containers":[{"name":"detector", "image":c["image"], "args":["serve",c["upstream"],c["baseline"],c["candidate"],c["crate_name"],job["repository"],job["revision"]],
            "wakeupProbe":{"httpGet":{"path":"/readyz","port":8080}}}],
        "snapshotConfig":{"goldenSnapshotConfig":{"mode":"GOLDEN_SNAPSHOT_MODE_DISABLED"}},
        "resources":{"limits":{"cpuMillicores":1000,"memoryBytes":1073741824u64}}});
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
        template["containers"][0]["env"] = json!([
            {"name":"DETECTOR_GIT_AUTH_HOST", "value":host},
            {"name":"GIT_SSL_CAINFO", "value":"/run/ate/egress-trust-bundle/trust-bundle.pem"}
        ]);
        template["containers"][0]["volumeMounts"] =
            json!([{"name":"system-info","mountPath":"/run/ate/egress-trust-bundle"}]);
        template["volumes"] = json!([{"name":"system-info","systemInfo":{"dataSources":[{"trustBundle":{"names":["egress-mitm.ate.dev","system-roots.ate.dev"],"path":"trust-bundle.pem"}}]}}]);
    }
    let path = std::env::temp_dir().join(format!("{name}.json"));
    fs::write(&path, template.to_string()).map_err(|e| e.to_string())?;
    let path_str = path.to_str().ok_or("invalid temporary path")?;
    let result = kubectl(&["create", "actor-template", "-f", path_str]);
    let _ = fs::remove_file(&path);
    result?;
    kubectl(&["create", "actor", &name, "-a", space, "--template", &name])?;
    let result = (|| {
        if let Some(policy) = c["egress_policy"].as_str() {
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
        let output = Command::new("curl")
            .args([
                "--fail-with-body",
                "--silent",
                "--show-error",
                "--retry",
                "20",
                "--retry-connrefused",
                "--retry-delay",
                "1",
                "--max-time",
                "300",
                "-X",
                "POST",
                &url,
            ])
            .output()
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
    let suspended = kubectl(&["suspend", "actor", &name, "-a", space, "--mode=sync"]);
    match (result, suspended) {
        (Ok(v), Ok(_)) => Ok(v),
        (Ok(_), Err(e)) => Err(format!("comparison completed but suspend failed: {e}")),
        (Err(e), _) => Err(e),
    }
}
