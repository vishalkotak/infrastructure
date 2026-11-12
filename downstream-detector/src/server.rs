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
    let address = env::var("DETECTOR_LISTEN").unwrap_or_else(|_| "0.0.0.0:8080".into());
    let report_path = PathBuf::from(
        env::var("DETECTOR_REPORT_PATH").unwrap_or_else(|_| "/tmp/downstream-report.txt".into()),
    );
    let listener = TcpListener::bind(&address)?;
    eprintln!("listening on {address}");
    for connection in listener.incoming() {
        let mut stream = connection?;
        stream.set_read_timeout(Some(std::time::Duration::from_secs(5)))?;
        let mut buf = [0; 4096];
        let n = match stream.read(&mut buf) {
            Ok(n) => n,
            Err(_) => continue,
        };
        let request = String::from_utf8_lossy(&buf[..n]);
        let request_line = request.lines().next().unwrap_or("");
        let discovery_crate = request_line
            .strip_prefix("GET /discover/")
            .and_then(|rest| {
                rest.strip_suffix(" HTTP/1.1")
                    .or_else(|| rest.strip_suffix(" HTTP/1.0"))
            });
        let (code, body) = match request_line {
            "GET /readyz HTTP/1.1" | "GET /readyz HTTP/1.0" => ("200 OK", "ready\n".to_string()),
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
            "POST /run HTTP/1.1" | "POST /run HTTP/1.0" => match compare(&config) {
                Ok(report) => match fs::write(report_path.with_extension("pending"), &report)
                    .and_then(|()| fs::rename(report_path.with_extension("pending"), &report_path))
                {
                    Ok(()) => ("200 OK", report),
                    Err(e) => (
                        "500 Internal Server Error",
                        format!("cannot save report: {e}\n"),
                    ),
                },
                Err(e) => (
                    "500 Internal Server Error",
                    format!("comparison failed: {e}\n"),
                ),
            },
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
        };
        let response = format!("HTTP/1.1 {code}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        if let Err(e) = stream.write_all(response.as_bytes()) {
            eprintln!("response failed: {e}");
        }
    }
    Ok(())
}
