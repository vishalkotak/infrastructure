use std::{
    io::{self, Read, Write},
    process::{Child, Command, Output, Stdio},
    thread,
    time::{Duration, Instant},
};

const READ_CHUNK: usize = 8192;

// Drain both pipes concurrently, but retain only a bounded prefix so a noisy
// test or a broken adapter cannot exhaust the controller/actor's memory.
pub fn run_limited(
    command: &mut Command,
    timeout: Duration,
    max_capture_bytes: usize,
) -> io::Result<Output> {
    prepare_process_group(command);
    let child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    wait_child_limited(child, timeout, max_capture_bytes)
}

pub fn run_with_stdin(
    command: &mut Command,
    input: &[u8],
    timeout: Duration,
    max_capture_bytes: usize,
) -> io::Result<Output> {
    prepare_process_group(command);
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let write_result = child.stdin.take().expect("piped stdin").write_all(input);
    if let Err(error) = write_result {
        terminate(&mut child);
        let _ = child.wait();
        return Err(error);
    }
    wait_child_limited(child, timeout, max_capture_bytes)
}

fn prepare_process_group(command: &mut Command) {
    // The actor server holds this control token only to authenticate its local
    // controller. Never expose it to git, Cargo, curl, kubectl, or adapters.
    command.env_remove("DETECTOR_RUN_TOKEN");
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
}

pub fn wait_child_limited(
    mut child: Child,
    timeout: Duration,
    max_capture_bytes: usize,
) -> io::Result<Output> {
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let stdout_reader = thread::spawn(move || read_bounded(stdout, max_capture_bytes));
    let stderr_reader = thread::spawn(move || read_bounded(stderr, max_capture_bytes));
    let started = Instant::now();
    let (status, timed_out) = match wait_until(&mut child, started, timeout) {
        Ok(result) => result,
        Err(error) => {
            terminate(&mut child);
            let _ = child.wait();
            return Err(error);
        }
    };
    let (mut stdout, stdout_truncated) = stdout_reader
        .join()
        .map_err(|_| io::Error::other("stdout reader panicked"))??;
    let (mut stderr, stderr_truncated) = stderr_reader
        .join()
        .map_err(|_| io::Error::other("stderr reader panicked"))??;
    if stdout_truncated {
        stdout.extend_from_slice(b"\n[output truncated at configured capture limit]\n");
    }
    if stderr_truncated {
        stderr.extend_from_slice(b"\n[output truncated at configured capture limit]\n");
    }
    if timed_out {
        stderr.extend_from_slice(b"\n[process timed out and was terminated]\n");
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn wait_until(
    child: &mut Child,
    started: Instant,
    timeout: Duration,
) -> io::Result<(std::process::ExitStatus, bool)> {
    loop {
        if let Some(status) = child.try_wait()? {
            kill_process_group(child.id());
            return Ok((status, false));
        }
        if started.elapsed() >= timeout {
            terminate(child);
            return child.wait().map(|status| (status, true));
        }
        thread::sleep(Duration::from_millis(50));
    }
}

#[cfg(unix)]
fn terminate(child: &mut Child) {
    kill_process_group(child.id());
    let _ = child.kill();
}

#[cfg(not(unix))]
fn terminate(child: &mut Child) {
    let _ = child.kill();
}

#[cfg(unix)]
fn kill_process_group(process_id: u32) {
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    const SIGKILL: i32 = 9;
    let group = -(process_id as i32);
    // Commands run in a dedicated process group so compiler and adapter child
    // processes are also terminated when the command exits or times out.
    let _ = unsafe { kill(group, SIGKILL) };
}

#[cfg(not(unix))]
fn kill_process_group(_process_id: u32) {}

fn read_bounded<R: Read>(mut reader: R, max: usize) -> io::Result<(Vec<u8>, bool)> {
    let mut captured = Vec::with_capacity(max.min(READ_CHUNK));
    let mut buffer = [0u8; READ_CHUNK];
    let mut truncated = false;
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let keep = max.saturating_sub(captured.len()).min(count);
        captured.extend_from_slice(&buffer[..keep]);
        truncated |= keep < count;
    }
    Ok((captured, truncated))
}

#[cfg(all(test, unix))]
mod tests {
    use super::run_limited;
    use std::{process::Command, time::Duration};

    #[test]
    fn bounds_captured_output_while_draining_child_pipes() {
        let mut command = Command::new("sh");
        command.args(["-c", "printf 123456789; printf abcdef >&2"]);
        let output = run_limited(&mut command, Duration::from_secs(2), 4).unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8_lossy(&output.stdout).starts_with("1234"));
        assert!(String::from_utf8_lossy(&output.stdout).contains("output truncated"));
        assert!(String::from_utf8_lossy(&output.stderr).starts_with("abcd"));
    }

    #[test]
    fn does_not_inherit_actor_control_token() {
        let mut command = Command::new("sh");
        command.args(["-c", "test -z \"$DETECTOR_RUN_TOKEN\""]);
        command.env("DETECTOR_RUN_TOKEN", "secret");
        let output = run_limited(&mut command, Duration::from_secs(2), 1024).unwrap();
        assert!(output.status.success());
    }

    #[test]
    fn enforces_timeout_and_terminates_child_process_group() {
        let mut command = Command::new("sh");
        command.args(["-c", "sleep 30 & wait"]);
        let started = std::time::Instant::now();
        let output = run_limited(&mut command, Duration::from_millis(150), 1024).unwrap();
        assert!(started.elapsed() < Duration::from_secs(5));
        assert!(String::from_utf8_lossy(&output.stderr).contains("process timed out"));
    }
}
