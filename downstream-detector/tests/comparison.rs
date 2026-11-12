use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::Path,
    process::Command,
    time::{SystemTime, UNIX_EPOCH},
};

fn git(repo: &Path, args: &[&str]) {
    let output = Command::new("git")
        .current_dir(repo)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn catches_a_real_downstream_regression() {
    let root = std::env::temp_dir().join(format!(
        "detector-test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let upstream = root.join("upstream");
    let downstream = root.join("downstream");
    fs::create_dir_all(upstream.join("src")).unwrap();
    fs::create_dir_all(downstream.join("src")).unwrap();
    fs::write(
        upstream.join("Cargo.toml"),
        "[package]\nname='example-math'\nversion='0.1.0'\nedition='2021'\n",
    )
    .unwrap();
    fs::write(
        upstream.join("src/lib.rs"),
        "pub fn double(x: i32) -> i32 { x * 2 }\n",
    )
    .unwrap();
    git(&upstream, &["init", "-q"]);
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
    );
    git(&upstream, &["tag", "baseline"]);
    fs::write(
        upstream.join("src/lib.rs"),
        "pub fn double(x: i32) -> i32 { x * 3 }\n",
    )
    .unwrap();
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "candidate",
        ],
    );
    fs::write(downstream.join("Cargo.toml"), "[package]\nname='client'\nversion='0.1.0'\nedition='2021'\n[dependencies]\nexample-math='0.1.0'\n").unwrap();
    fs::write(
        downstream.join("src/lib.rs"),
        "#[test] fn works() { assert_eq!(example_math::double(2), 4); }\n",
    )
    .unwrap();
    git(&downstream, &["init", "-q"]);
    git(&downstream, &["add", "."]);
    git(
        &downstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "client",
        ],
    );
    let output = Command::new(env!("CARGO_BIN_EXE_downstream-detector"))
        .args([
            "compare",
            upstream.to_str().unwrap(),
            "baseline",
            "HEAD",
            "example-math",
            downstream.to_str().unwrap(),
            "HEAD",
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = String::from_utf8_lossy(&output.stdout);
    assert!(report.contains("POTENTIAL_REGRESSION"), "{report}");
    assert!(
        report.contains("test tests::works ... FAILED") || report.contains("test works ... FAILED"),
        "{report}"
    );

    // Exercise the actor's HTTP interface with the same real Git repositories.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    drop(listener);
    let mut server = Command::new(env!("CARGO_BIN_EXE_downstream-detector"))
        .args([
            "serve",
            upstream.to_str().unwrap(),
            "baseline",
            "HEAD",
            "example-math",
            downstream.to_str().unwrap(),
            "HEAD",
        ])
        .env("DETECTOR_LISTEN", address.to_string())
        .env("DETECTOR_REPORT_PATH", root.join("report.txt"))
        .env("DETECTOR_RUN_TOKEN", "a".repeat(64))
        .spawn()
        .unwrap();
    let result = || {
        let mut ready = false;
        for _ in 0..100 {
            if let Ok(response) =
                request(address, "GET /readyz HTTP/1.1\r\nHost: localhost\r\n\r\n")
            {
                if response.contains("200 OK") {
                    ready = true;
                    break;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(ready, "actor did not become ready");
        let response = request(
            address,
            "POST /run HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\nContent-Length: 0\r\n\r\n",
        )
        .unwrap();
        assert!(response.contains("POTENTIAL_REGRESSION"), "{response}");
        let report = request(address, "GET /report HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\n\r\n").unwrap();
        assert!(report.contains("POTENTIAL_REGRESSION"), "{report}");
    };
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(result));
    let _ = server.kill();
    let _ = server.wait();
    if let Err(payload) = outcome {
        std::panic::resume_unwind(payload);
    }
    // A candidate that cannot compile must not be reported as a failed test.
    fs::write(upstream.join("src/lib.rs"), "this is not valid Rust\n").unwrap();
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "broken build",
        ],
    );
    let failed_build = Command::new(env!("CARGO_BIN_EXE_downstream-detector"))
        .args([
            "compare",
            upstream.to_str().unwrap(),
            "baseline",
            "HEAD",
            "example-math",
            downstream.to_str().unwrap(),
            "HEAD",
        ])
        .output()
        .unwrap();
    let report = String::from_utf8_lossy(&failed_build.stdout);
    assert!(
        report.contains("INCONCLUSIVE_CANDIDATE_BUILD_OR_SETUP_FAILED"),
        "{report}"
    );
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn catches_a_python_downstream_regression() {
    let root = std::env::temp_dir().join(format!(
        "detector-python-test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let upstream = root.join("upstream");
    let downstream = root.join("downstream");
    fs::create_dir_all(upstream.join("src/example_pkg")).unwrap();
    fs::create_dir_all(downstream.join("tests")).unwrap();
    fs::write(
        upstream.join("pyproject.toml"),
        "[project]\nname = 'example.pkg'\nversion = '0.1.0'\n",
    )
    .unwrap();
    fs::write(
        upstream.join("src/example_pkg/__init__.py"),
        "def answer():\n    return 42\n",
    )
    .unwrap();
    git(&upstream, &["init", "-q"]);
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
    );
    git(&upstream, &["tag", "baseline"]);
    fs::write(
        upstream.join("src/example_pkg/__init__.py"),
        "def answer():\n    return 41\n",
    )
    .unwrap();
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "candidate",
        ],
    );
    fs::write(downstream.join("tests/__init__.py"), "").unwrap();
    fs::write(downstream.join("tests/test_api.py"), "import unittest\nfrom example_pkg import answer\n\nclass ApiTest(unittest.TestCase):\n    def test_answer(self):\n        self.assertEqual(answer(), 42)\n").unwrap();
    git(&downstream, &["init", "-q"]);
    git(&downstream, &["add", "."]);
    git(
        &downstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "consumer",
        ],
    );
    let output = Command::new(env!("CARGO_BIN_EXE_downstream-detector"))
        .args([
            "compare",
            upstream.to_str().unwrap(),
            "baseline",
            "HEAD",
            "example.pkg",
            downstream.to_str().unwrap(),
            "HEAD",
        ])
        .env("DETECTOR_ECOSYSTEM", "python")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = String::from_utf8_lossy(&output.stdout);
    assert!(report.contains("POTENTIAL_REGRESSION"), "{report}");
    assert!(report.contains("test_answer"), "{report}");
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn catches_an_npm_downstream_regression() {
    if Command::new("npm").arg("--version").output().is_err() {
        return;
    }
    let root = std::env::temp_dir().join(format!(
        "detector-npm-test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let upstream = root.join("upstream");
    let downstream = root.join("downstream");
    fs::create_dir_all(&upstream).unwrap();
    fs::create_dir_all(&downstream).unwrap();
    fs::write(
        upstream.join("package.json"),
        r#"{"name":"example-math","version":"1.0.0","main":"index.js"}"#,
    )
    .unwrap();
    fs::write(upstream.join("index.js"), "exports.double = x => x * 2;\n").unwrap();
    git(&upstream, &["init", "-q"]);
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
    );
    git(&upstream, &["tag", "baseline"]);
    fs::write(upstream.join("index.js"), "exports.double = x => x * 3;\n").unwrap();
    git(&upstream, &["add", "."]);
    git(
        &upstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "candidate",
        ],
    );
    fs::write(
        downstream.join("package.json"),
        r#"{"name":"consumer","version":"1.0.0","scripts":{"test":"node test.js"}}"#,
    )
    .unwrap();
    fs::write(downstream.join("test.js"), "const assert = require('node:assert/strict'); const {double} = require('example-math'); assert.equal(double(2), 4);\n").unwrap();
    git(&downstream, &["init", "-q"]);
    git(&downstream, &["add", "."]);
    git(
        &downstream,
        &[
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "consumer",
        ],
    );
    let output = Command::new(env!("CARGO_BIN_EXE_downstream-detector"))
        .args([
            "compare",
            upstream.to_str().unwrap(),
            "baseline",
            "HEAD",
            "example-math",
            downstream.to_str().unwrap(),
            "HEAD",
        ])
        .env("DETECTOR_ECOSYSTEM", "npm")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report = String::from_utf8_lossy(&output.stdout);
    assert!(report.contains("POTENTIAL_REGRESSION"), "{report}");
    assert!(
        report.contains("install changed package source"),
        "{report}"
    );
    fs::remove_dir_all(root).unwrap();
}

fn request(address: std::net::SocketAddr, request: &str) -> std::io::Result<String> {
    let mut stream = TcpStream::connect(address)?;
    stream.write_all(request.as_bytes())?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    Ok(response)
}
