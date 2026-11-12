# Downstream compatibility detector

A small Rust prototype that compares one downstream Cargo project's tests against two revisions of one upstream crate. It can run as an Agent Substrate actor, or locally for development.

## Live demo: detect a downstream break in Agent Substrate

[![Watch the narrated walkthrough](demo/itoa-serde/walkthrough.gif)](demo/itoa-serde/walkthrough.mp4)

[Play or download the 36-second narrated walkthrough](demo/itoa-serde/walkthrough.mp4) · [Video poster](demo/itoa-serde/walkthrough-poster.png)

Regenerate the video on macOS with Swift, FFmpeg, and the built-in `say` voice:

```sh
bash demo/itoa-serde/make-video.sh
```

This demo uses real, pinned open-source projects: [`itoa`](https://github.com/dtolnay/itoa) as the upstream crate and [`serde_json`](https://github.com/serde-rs/json) as its consumer. The demo image starts with the original `itoa` revision, then applies a one-line change that makes formatting the integer `1` return `"2"`. The public Rust API and types stay unchanged, but the behavior is wrong. The public repositories are not modified.

The run happened inside an Agent Substrate actor on the local Kind cluster. Its comparison ran with network access disabled; both Git repositories and the locked Cargo dependency cache were prepared in the image. The baseline passed. With the candidate, `serde_json`'s debug integration test target had **3 failures and 6 passes**. Cargo stopped at that failing target, so this result does not claim that the later candidate test targets ran. The detector returned `POTENTIAL_REGRESSION` based on actual test assertion failures, not a build or sandbox setup error.

| | Pinned input |
| --- | --- |
| Upstream baseline | `itoa` `9339ca99a43c550b5ee36e2afe524d1c90413461` |
| Upstream candidate | `itoa` `a1e3057dc9a66b3aa2d1197d5cf7e64d2b5eeaa5` |
| Downstream consumer | `serde_json` `743dd32ae79719a5817944f3c5b0caa3340b5657` |
| Substrate actor | `compatibility/itoa-serde-fixed-2` |

**Run result:** `POTENTIAL_REGRESSION` · baseline passed · candidate failed 3 downstream assertions.

<details>
<summary>Failure excerpt (full backtraces are in the report)</summary>

```text
---- number stdout ----
thread 'number' panicked at tests/debug.rs:8:5:
assertion `left == right` failed
  left: "Number(2)"
 right: "Number(1)"

---- indented stdout ----
thread 'indented' panicked at tests/debug.rs:82:5:
left: ... Number(2) ...
right: ... Number(1) ...

---- value_number stdout ----
thread 'value_number' panicked at tests/debug.rs:29:5:
left: "Number(2)"
right: "Number(1)"

test result: FAILED. 6 passed; 3 failed; 0 ignored; 0 measured; 0 filtered out
```

</details>

The [complete actor report](demo/itoa-serde/report.txt) contains the baseline and candidate Cargo output and all three full Rust backtraces. [Run metadata](demo/itoa-serde/run-metadata.txt) records the image digest, actor and pinned revisions. The intentionally faulty one-line change is available as a [patch](demo/itoa-serde/change.patch); the [image recipe](demo/itoa-serde/Dockerfile) and [preparation script](demo/itoa-serde/prepare.sh) show how the pinned repos and offline dependency cache are assembled.

To reproduce locally, build the detector image first, then build and run the demo image:

```sh
docker build -t downstream-detector:latest .
docker build -f demo/itoa-serde/Dockerfile -t downstream-detector:itoa-serde-demo .
docker run --rm --network none --memory 2g --cpus 2 \
  downstream-detector:itoa-serde-demo compare \
  /opt/real-demo/itoa demo-baseline HEAD itoa \
  /opt/real-demo/serde-json HEAD
```

The same image can be pushed to a registry reachable by a Substrate worker, then referenced by digest in an actor template. The Docker recipe fetches the pinned public repositories and Cargo dependencies during image build; the actual comparison in the actor is offline. `POTENTIAL_REGRESSION` is a signal to inspect the logs, not a proof that every consumer will fail.

## Read the code

1. `src/main.rs`: parses the command line and selects `compare` or `serve`.
2. `src/detector.rs`: checks out clean copies, patches the dependency, runs tests, and classifies the results.
3. `src/server.rs`: exposes `/readyz` and `/run` for an actor with a fixed operator-supplied configuration.
4. `tests/comparison.rs`: builds two small Git repositories and verifies that a real regression is detected.

## Run the test

Requirements: Rust, Cargo, Git, and curl. Building needs the dependencies in `Cargo.lock`; fetch them once with `cargo fetch` before working offline.

```sh
cargo test --offline
```

The integration test creates a library that changes `double(2)` from 4 to 6 and a downstream project that expects 4. It runs the actual detector and checks for `POTENTIAL_REGRESSION`.

## Discover downstream crates

```sh
export CRATES_IO_USER_AGENT='your-application/0.1 (you@example.com)'
cargo run -- discover itoa
```

Discovery queries every page of crates.io's reverse-dependency API and returns JSON with each dependent crate's published version, repository when provided, version requirement, dependency kind, and optional/target fields. Identify your application and contact in the user agent as required by crates.io; it is sent with every request. The tool paces requests and fails rather than returning partial results when a request or response fails. A registry changing during pagination can still lead to duplicates or omissions.

To rank public candidates by registry downloads, deduplicate repositories and cap the number selected:

```sh
cargo run -- discover itoa > /tmp/itoa-dependents.json
cargo run -- plan /tmp/itoa-dependents.json 10
```

The plan does not run any tests. Download counts are rough public popularity signals, not measures of importance to your company. It deliberately leaves revision empty: a repository's current HEAD is not necessarily the published crate version. Confirm a commit and opt-in for every repository before constructing a batch.

The results are **direct, public, published crates.io dependents**, not a list of every application in the world: private code, unpublished projects, packages from other registries, and transitive dependents are not included. Entries are candidates, not permission to run code; repository metadata may be missing, point to a monorepo, or not identify the exact published source. Discovery does not automatically enqueue or test the returned repositories.

The actor also exposes `GET /discover/<crate-name>` when `CRATES_IO_USER_AGENT` is set in its container environment and its egress policy allows `crates.io`. Discovery is a synchronous request, which may take a long time for popular crates. It does not run during the offline demo.

## Run a bounded batch in parallel

The `batch` command reads an explicitly approved inventory like `examples/batch.json`, orders jobs by integer priority (highest first), selects at most `max_jobs`, and runs up to `max_parallel` comparisons at once. Each comparison gets a separate Substrate actor with its own configured repository and revision. It creates an actor template and actor, applies the optional egress policy, resumes the actor, requests a comparison, and synchronously suspends the actor. The report includes each job's status and the number omitted by the job limit.

```sh
cargo run -- batch /path/to/approved-batch.json > batch-report.json
```

Run the controller in an environment with authenticated `kubectl ate`, access to the selected atespace, and enough ready workers labeled `workload: rust-detector`. Set `KUBECONFIG` to the intended cluster. The worker pool determines actual capacity: `max_parallel` must not exceed the available workers for this demo. Workers may be shared with other workloads; resource exhaustion and setup failures are reported per job rather than automatically retried. The limit controls job count, not a dollar budget or total execution time. The controller retains templates and suspended actors after the run; operators must manage their lifecycle and storage. A timed-out request does not yet cancel the underlying test. Do not use this prototype as a production job scheduler.

## Private GitHub Enterprise Server

The public registry cannot see a company's private dependency graph. Supply the approved private repositories, pinned revisions and company priorities in the batch inventory. A higher priority number might represent a production-critical service, revenue impact or release risk. Automatic GitHub Enterprise organization discovery, webhooks, and private registry dependency resolution are **not implemented**.

For a private Git server, replace the placeholders in `substrate/enterprise-egress.example.yaml`, set `git_auth_host` and `egress_policy` in the batch, configure Substrate's credential provider and namespace authorization, and store a short-lived, read-only GitHub token in the configured secret store. The actor sends an `Authorization` placeholder only to that Git host; the egress gateway replaces it with the secret. The actor mounts the gateway's trust bundle and sets `GIT_SSL_CAINFO`. GitHub Enterprise Server must be reachable from the cluster, and an enterprise-specific CA may require additional trust configuration. Never put tokens in repository URLs or the batch file. See [Substrate credential injection](https://ate.dev/docs/guides/egress-credential-injection/) and [its trust-bundle guide](https://ate.dev/docs/guides/egress-trust-bundle/).

This integration is configurable but has **not been tested against a private GitHub Enterprise Server or credential provider**. Git credentials are not in actor environment variables, but untrusted code can still use the authorized gateway path; scope credentials and network access tightly. `patch_source` can optionally specify the Cargo registry index URL for patching an alternative registry; private registry configuration and packages must already exist in the actor image, and that path is untested. The comparison currently expects root-level manifests and offline Cargo; Git-sourced, workspace and uncached dependencies need additional support. Therefore the prototype does not yet support every private Rust project.

## Compare your own repositories

```sh
cargo run --offline -- compare \
  /path/to/upstream-git BASELINE_COMMIT CANDIDATE_COMMIT crate-name \
  /path/to/downstream-git DOWNSTREAM_COMMIT
```

Git repository sources may also be HTTPS URLs if the execution environment permits access. Prefer immutable commit hashes. The upstream crate must be at the root of its repository, and the downstream project's Cargo manifest must be at its repository root. The downstream dependency must resolve from crates.io, and its version requirement must accept the checked-out crate version. Cargo's `[patch.crates-io]` override replaces it for the test. Separate clean checkouts and separate Cargo target directories are used for each side.

Verdicts:

- `POTENTIAL_REGRESSION`: baseline passed and candidate failed. The candidate failure can also be environmental; inspect its logs.
- `NO_REGRESSION_DETECTED`: both passed. This only covers the tests actually executed, not all possible breakages.
- `INCONCLUSIVE_BASELINE_FAILED`: the baseline failed, so the comparison cannot establish a regression.
- `INCONCLUSIVE_CANDIDATE_BUILD_OR_SETUP_FAILED`: the candidate failed before a test failure was observed; this can be a compilation error or an infrastructure problem, so inspect the logs.

Cargo runs with `--offline`; dependencies needed by the downstream project must already be present in its Cargo cache or vendored/configured in the repository. An uncached dependency, unsupported Git dependency override, or other setup issue can cause failure. The detector rejects Cargo's unused-patch warning rather than silently testing the original dependency. Tests may be flaky; a single pass/fail difference is not proof of causation.

## Run as a Substrate actor

The Docker image includes a small demonstration pair of Git repositories. The baseline passes; the candidate breaks the downstream test. No egress or crate download is needed for this demo.

Prerequisites: Docker with a running daemon, an existing Agent Substrate installation, `kubectl ate`, and a ready worker pool with enough CPU and memory. This project does not install Substrate. See the [Substrate quickstart](https://ate.dev/docs/get-started/quickstart/) for the installation prerequisites. `substrate/worker-pool.yaml` demonstrates a suitable pool; its worker image is specific to the local installation and must be replaced for other clusters. The actor template selects pools labeled `workload: rust-detector`.

Build and push the image to a registry accessible from the cluster:

```sh
docker build -t YOUR_REGISTRY/downstream-detector:demo .
docker push YOUR_REGISTRY/downstream-detector:demo
```

Copy the **image manifest digest returned by the push** and replace the `image` in `substrate/actor-template.yaml` with `YOUR_REGISTRY/downstream-detector@sha256:...`. The placeholder is deliberately invalid. The node pulling the image needs access to your registry. A local image tag alone is not a digest reference and is not enough for this template.

Create the actor:

```sh
kubectl ate create atespace compatibility
kubectl ate create actor-template -f substrate/actor-template.yaml
kubectl ate create actor rust-detector-1 -a compatibility --template rust-detector
kubectl ate proxy actor rust-detector-1 8080:8080 -a compatibility
```

In another shell:

```sh
curl -X POST http://localhost:8080/run
curl http://localhost:8080/report
```

`/run` executes a comparison synchronously and returns a text report. `/report` reads the most recent completed report from the actor filesystem. `/readyz` provides a wakeup probe. The actor handles one request at a time.

To exercise Substrate's state lifecycle, after `/run` succeeds, suspend the actor synchronously and resume it, then use the proxy again to fetch `/report`:

```sh
kubectl ate suspend actor rust-detector-1 -a compatibility --mode=sync
kubectl ate resume actor rust-detector-1 -a compatibility
curl http://localhost:8080/report
```

If the proxy has exited, start it again before the final `curl`. The report is saved on the actor filesystem, which Substrate snapshots; the synchronous suspend waits for the snapshot to reach storage. The demo comparison and fetching the saved report after suspend and request-triggered resume were verified on a local Kind cluster using gVisor. The report is not independently backed up or stored externally.

For real repositories, replace the image command arguments with `serve`, upstream URL, baseline ref, candidate ref, crate name, downstream URL, and downstream ref, in that order. Those arguments are fixed at actor-template creation. Network access is denied by default; configure an actor egress policy and the appropriate Substrate egress gateway before using remote Git. Cargo runs offline, so required dependencies must be baked into the image or supplied through a preconfigured local cache. Private repository credentials and other dependency sources are not implemented.

The optional `substrate/egress-policy.yaml` allows TLS access to crates.io for discovery and github.com for Git clones. For discovery, add `CRATES_IO_USER_AGENT` with your contact details to the container's environment in a new actor template, then create the policy for that actor:

```sh
kubectl ate create egress-policy rust-detector-1 -a compatibility -f substrate/egress-policy.yaml
curl http://localhost:8080/discover/itoa
```

When run through the actor, Git clones and test execution happen in its sandbox, not on the machine running `kubectl`. Allowing a domain also permits untrusted code in that actor to contact it; a network allowlist is not a complete data-exfiltration defense. Baseline and candidate currently share one actor and security boundary, and private credentials should not be placed inside it. For untrusted repositories, use separate disposable actors per test run, narrow egress, enforce time and resource limits, and avoid access to sensitive data. Running the `compare` or `discover` CLI directly runs them on your host, not in Substrate.

## Boundaries of this prototype

- It compares one configured downstream repository per actor, only Rust/Cargo and crates.io-sourced dependencies. Discovery identifies published direct dependents but does not yet watch releases, create actors automatically, or publish reports.
- It executes repository build scripts and tests as code. Run only opted-in, trusted repositories in a properly configured isolated sandbox. The HTTP server has no application-level authentication or request timeout and must not be exposed publicly; rely on Substrate's authenticated router and operator-controlled access.
- Baseline and candidate run in separate processes and directories **inside the same actor**, not in separate security boundaries. A malicious test can affect other files or processes in that actor. Use separate actors and stronger resource/time limits for untrusted workloads.
- A passing test suite is not proof of compatibility, and Cargo's version resolution, build scripts, platform differences, missing dependencies and test flakiness can influence results. This is a development prototype, not a security boundary or a production service.
