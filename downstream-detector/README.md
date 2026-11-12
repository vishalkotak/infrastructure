# Downstream Detector

Checks whether a proposed Rust dependency change breaks the software that depends on it.

## Why I built it

I work on platform problems where one shared library serves many product teams. A change can keep compiling while subtly breaking a consumer. I wanted a way to submit a pull request and learn which downstream projects fail before the change ships.

## How to use it

The PR checker needs a one-time setup of the execution backend, credentials, and (for a private company) organization settings. After that, pass it the pull request URL:

```sh
downstream-detector check https://github.com/OWNER/REPO/pull/123
```

It identifies the changed Rust crate, discovers and ranks consumers, runs their tests in isolated jobs, and prints a short result summary. Full test output and backtraces are saved under `.downstream-detector/reports/`. Public consumer discovery uses crates.io; set `DETECTOR_GITHUB_ORG` and a GitHub token to include company repositories found through code search. For GitHub Enterprise Server, also set `DETECTOR_GITHUB_HOST`. The tool does not automatically configure company credentials or an internal Cargo registry.

Execution is swappable. `DETECTOR_EXECUTOR=substrate` uses the built-in Google Agent Substrate adapter. To use another environment, set `DETECTOR_EXECUTOR=command` and point `DETECTOR_EXECUTOR_COMMAND` at a trusted adapter executable. The CLI, discovery, ranking, budgets, and reports stay the same; the adapter provisions the job in your chosen runner. See the [executor adapter protocol](docs/executor-adapter.md).

### Run the local demo

With Docker running, run:

```sh
bash demo/itoa-serde/run.sh
```

This builds the detector and a self-contained demo image, then checks an intentionally behavior-breaking change to `itoa` against `serde_json`. The public API is unchanged, but downstream tests catch the changed output. The actor runs with network disabled. See the [captured output and backtraces](demo/itoa-serde/report.txt), the [demo change](demo/itoa-serde/change.patch), or watch the walkthrough below.

### When there are thousands of consumers

The checker does not start a sandbox for every discovered repository. It first deduplicates consumers by repository, ranks them, and applies a job budget. Public crates are ranked by crates.io download counts; company repositories can be ranked with `DETECTOR_PRIORITY_FILE`, a JSON map from repository name to an integer priority. Company repositories are considered before public ones when both sources are enabled. These are useful starting signals, not a guarantee that the selected repositories are the most business-critical.

By default it selects at most **5 repositories** (`DETECTOR_MAX_JOBS`, configurable up to 32) and runs at most **2 checks at once** (`DETECTOR_MAX_PARALLEL`, configurable up to 32). Each selected repository gets its own temporary actor, so the number of sandboxes is bounded by the job budget, while parallelism bounds active resource use and cost. The remaining consumers are omitted from that run; reports include the selected count and any repositories that could not be prepared. The current prototype samples by ranking rather than scheduling every consumer in batches. Company code search is paginated up to 1,000 results, so larger organizations need a broader inventory source for complete discovery.

## How Agent Substrate fits

The built-in backend runs each downstream test in an isolated actor scheduled on a worker pool, with CPU and memory limits, network policy, and retained logs. The same compatibility-check request can be sent through the adapter protocol to a different job system. Agent Substrate documents warm actor resume under 500 ms and over 500 activations per second; those are platform figures, not this demo's benchmark ([architecture](https://ate.dev/docs/concepts/architecture/)).

In the recorded local Kind/gVisor run, an actor had **1 vCPU and 1 GiB RAM**. Cargo reported **49.18 s** to build the baseline, **48.78 s** for its doc tests, and **42.64 s** to build the candidate; the failing candidate test target finished in **0.12 s**. These are individual phases, not end-to-end latency, and vary with hardware and cache state. See the [full run report](demo/itoa-serde/report.txt).

## Video demo

[![Watch the narrated walkthrough](demo/itoa-serde/walkthrough.gif)](demo/itoa-serde/walkthrough.mp4)

[Play or download the 36-second video](demo/itoa-serde/walkthrough.mp4).
