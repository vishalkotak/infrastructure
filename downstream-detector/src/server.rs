use crate::detector::{compare, Config};
use crate::discovery;
use std::{
    env, fs,
    io::{Read, Write},
    net::TcpListener,
    path::PathBuf,
};

// The actor has one operator-supplied configuration. Requests cannot supply code,
// repository URLs, or shell commands. This small server handles one run at a time.
pub fn serve(config: Config) -> Result<(), Box<dyn std::error::Error>> {
    protect_process_environment()?;
    let run_token = env::var("DETECTOR_RUN_TOKEN")
        .map_err(|_| "DETECTOR_RUN_TOKEN is required to protect actor endpoints")?;
    if run_token.len() != 64 || !run_token.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err("DETECTOR_RUN_TOKEN must be 32 random bytes encoded as hex".into());
    }
    let address = env::var("DETECTOR_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into());
    let report_path = PathBuf::from(
        env::var("DETECTOR_REPORT_PATH").unwrap_or_else(|_| "/tmp/downstream-report.txt".into()),
    );
    let listener = TcpListener::bind(&address)?;
    let mut run_response: Option<(&'static str, String)> = None;
    eprintln!("listening on {address}");
    for connection in listener.incoming() {
        let mut stream = connection?;
        stream.set_read_timeout(Some(std::time::Duration::from_secs(1)))?;
        let request = match read_headers(&mut stream) {
            Ok(request) => request,
            Err(message) => {
                let body = format!("{message}\n");
                let response = format!("HTTP/1.1 400 Bad Request\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                let _ = stream.write_all(response.as_bytes());
                continue;
            }
        };
        let request_line = request.lines().next().unwrap_or("");
        let discovery_crate = request_line
            .strip_prefix("GET /discover/")
            .and_then(|rest| {
                rest.strip_suffix(" HTTP/1.1")
                    .or_else(|| rest.strip_suffix(" HTTP/1.0"))
            });
        let is_ready = matches!(
            request_line,
            "GET /readyz HTTP/1.1" | "GET /readyz HTTP/1.0"
        );
        let authorized = is_ready || valid_authorization(&request, &run_token);
        let (code, body) = if !authorized {
            ("401 Unauthorized", "unauthorized\n".to_string())
        } else {
            match request_line {
                "GET /readyz HTTP/1.1" | "GET /readyz HTTP/1.0" => {
                    ("200 OK", "ready\n".to_string())
                }
                "GET /report HTTP/1.1" | "GET /report HTTP/1.0" => {
                    match fs::read_to_string(&report_path) {
                        Ok(report) => ("200 OK", report),
                        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                            ("404 Not Found", "no completed comparison\n".to_string())
                        }
                        Err(e) => (
                            "500 Internal Server Error",
                            format!("cannot read report: {e}\n"),
                        ),
                    }
                }
                "POST /run HTTP/1.1" | "POST /run HTTP/1.0" => {
                    if let Some(response) = &run_response {
                        (response.0, response.1.clone())
                    } else {
                        let response = match compare(&config) {
                            Ok(report) => {
                                match atomic_private_write(&report_path, report.as_bytes()) {
                                    Ok(()) => ("200 OK", report),
                                    Err(e) => (
                                        "500 Internal Server Error",
                                        format!("cannot save report: {e}\n"),
                                    ),
                                }
                            }
                            Err(e) => (
                                "500 Internal Server Error",
                                format!("comparison failed: {e}\n"),
                            ),
                        };
                        run_response = Some((response.0, response.1.clone()));
                        response
                    }
                }
                _ if discovery_crate.is_some() => match env::var("CRATES_IO_USER_AGENT") {
                    Ok(user_agent) => {
                        match discovery::discover(discovery_crate.unwrap(), &user_agent) {
                            Ok(report) => ("200 OK", report),
                            Err(e) => ("502 Bad Gateway", format!("discovery failed: {e}\n")),
                        }
                    }
                    Err(_) => (
                        "503 Service Unavailable",
                        "set CRATES_IO_USER_AGENT on the actor\n".to_string(),
                    ),
                },
                _ => ("404 Not Found", "not found\n".to_string()),
            }
        };
        let response = format!("HTTP/1.1 {code}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        if let Err(e) = stream.write_all(response.as_bytes()) {
            eprintln!("response failed: {e}");
        }
    }
    Ok(())
}

fn atomic_private_write(path: &std::path::Path, contents: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| std::path::Path::new("."));
    fs::create_dir_all(parent)?;
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let pending = parent.join(format!(".{name}.{}.{}.pending", std::process::id(), nonce));
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options.open(&pending)?;
        file.write_all(contents)?;
        file.sync_all()?;
        fs::rename(&pending, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(pending);
    }
    result
}

fn valid_authorization(request: &str, expected: &str) -> bool {
    let provided = request.lines().skip(1).find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("authorization")
            .then(|| value.trim().strip_prefix("Bearer "))
            .flatten()
    });
    let Some(provided) = provided else {
        return false;
    };
    if provided.len() != expected.len() {
        return false;
    }
    provided
        .bytes()
        .zip(expected.bytes())
        .fold(0u8, |difference, (left, right)| difference | (left ^ right))
        == 0
}

fn read_headers(stream: &mut std::net::TcpStream) -> Result<String, &'static str> {
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    let mut bytes = Vec::with_capacity(512);
    let mut buffer = [0u8; 512];
    loop {
        if bytes.len() >= 8192 || std::time::Instant::now() >= deadline {
            return Err("request headers too large or timed out");
        }
        match stream.read(&mut buffer) {
            Ok(0) => return Err("incomplete request headers"),
            Ok(count) => bytes.extend_from_slice(&buffer[..count]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                return Err("request headers too large or timed out")
            }
            Err(_) => return Err("could not read request headers"),
        }
        if bytes.windows(4).any(|window| window == b"\r\n\r\n") {
            return Ok(String::from_utf8_lossy(&bytes).into_owned());
        }
    }
}

#[cfg(target_os = "linux")]
fn protect_process_environment() -> Result<(), std::io::Error> {
    unsafe extern "C" {
        fn prctl(option: i32, arg2: usize, arg3: usize, arg4: usize, arg5: usize) -> i32;
    }
    const PR_SET_DUMPABLE: i32 = 4;
    let result = unsafe { prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(not(target_os = "linux"))]
fn protect_process_environment() -> Result<(), std::io::Error> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{atomic_private_write, valid_authorization};

    #[test]
    fn bearer_token_is_required_and_compared_exactly() {
        let request = "GET /report HTTP/1.1\r\nAuthorization: Bearer abc123\r\n\r\n";
        assert!(valid_authorization(request, "abc123"));
        assert!(!valid_authorization(request, "abc124"));
        assert!(!valid_authorization(
            "GET /report HTTP/1.1\r\n\r\n",
            "abc123"
        ));
    }

    #[test]
    fn report_write_replaces_symlinks_without_following_them() {
        let root =
            std::env::temp_dir().join(format!("detector-server-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir(&root).unwrap();
        let target = root.join("target");
        let report = root.join("report");
        std::fs::write(&target, "keep").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&target, &report).unwrap();
        #[cfg(not(unix))]
        std::fs::write(&report, "old").unwrap();
        atomic_private_write(&report, b"new report").unwrap();
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "keep");
        assert_eq!(std::fs::read_to_string(&report).unwrap(), "new report");
        let _ = std::fs::remove_dir_all(root);
    }
}
