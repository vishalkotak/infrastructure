# Downstream Detector

**Catch the break before your users do.** A Rust crate can keep the same API and still break the apps that depend on it. Downstream Detector takes a pull request, finds real consumers, runs their tests in isolated jobs, and shows you what failed.

## Why I built it

Platform teams ship shared libraries to many product teams. Reviewing the crate alone can't tell you whether those teams' software still works. I wanted one PR link to trigger that check automatically.

## Try it

```sh
downstream-detector check https://github.com/OWNER/REPO/pull/123
```

It discovers and ranks consumers, runs their tests, and saves full logs. Setup is one-time; see [private Git credentials](docs/private-git-credentials.md) and the [executor adapter](docs/executor-adapter.md) for backend options.

## See it catch a break

Run the self-contained demo with Docker:

```sh
bash demo/itoa-serde/run.sh
```

The demo changes `itoa` behavior without changing its API. `serde_json` still compiles, but its tests catch the break.

[![Watch the 36-second demo](demo/itoa-serde/walkthrough.gif)](demo/itoa-serde/walkthrough.mp4)

## Built for many consumers

It doesn't start a sandbox for every repository. Consumers are deduplicated and ranked, then checked within a budget: **5 jobs by default, 2 at a time** (up to 32 each). Public crates use download counts; company repos can use your priority list. This is a ranked sample, not complete coverage of every consumer.

## Why Agent Substrate

Substrate gives each check an isolated actor with resource limits, egress controls, and retained logs. Its warm actor resume is documented at under 500 ms; that is a platform figure, not this demo's benchmark ([architecture](https://ate.dev/docs/concepts/architecture/)). In the recorded local run, baseline and candidate builds each took about 49 and 43 seconds respectively; see the [full logs](demo/itoa-serde/report.txt). The execution backend is swappable.
