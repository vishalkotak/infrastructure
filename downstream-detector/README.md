# Downstream Detector

Checks whether a proposed Rust dependency change breaks the software that depends on it.

## Why I built it

I approached this as a platform team problem: we maintain shared libraries used by many product teams, and a change can preserve the API while quietly changing behavior. Before releasing a shared crate, I want to know which important downstream services fail their own tests, and why.

## How to use it

For the pinned `itoa` → `serde_json` demo, run one command:

```sh
bash demo/itoa-serde/run.sh
```

The intended workflow is to submit a candidate commit and get back the affected consumers and failing tests. This prototype demonstrates the comparison, but still uses preconfigured repositories and revisions; automatic pull request intake and consumer selection are not implemented yet.

## How Agent Substrate fits

Each downstream test run is a short, CPU and memory intensive job. Google's Agent Substrate gives it an isolated actor, a worker pool to schedule jobs across, resource limits, network policy, and snapshots for keeping reports after a run. That makes parallel checks practical without keeping a separate worker alive for every service. The platform documentation reports warm actor resume under 500 ms and over 500 activations per second; those are Substrate figures, not measurements from this demo ([architecture](https://ate.dev/docs/concepts/architecture/)).

In our recorded local Kind/gVisor run, the actor was limited to **1 vCPU and 1 GiB RAM**. Cargo reported **49.18 s** to build the baseline, **48.78 s** for its doc tests, and **42.64 s** to build the candidate; the candidate's failing test target finished in **0.12 s**. These are individual phases, not end-to-end latency, and will vary with hardware and cache state. The full [run report](demo/itoa-serde/report.txt) has the logs and backtraces.

## Video demo

[![Watch the narrated walkthrough](demo/itoa-serde/walkthrough.gif)](demo/itoa-serde/walkthrough.mp4)

[Play or download the 36-second video](demo/itoa-serde/walkthrough.mp4).
