# Downstream Detector

Checks whether an upstream Rust change breaks a downstream project's tests.

## Why we built it

An API can stay unchanged while a behavior change breaks the applications that rely on it. We built this to test a proposed crate revision against a real consumer before release, and return the failing tests so maintainers can judge the impact.

## How to use it

The included demo compares pinned revisions of `itoa` and its consumer, `serde_json`. Build the images (the demo image fetches its pinned inputs), then run the comparison offline:

```sh
docker build -t downstream-detector:latest .
docker build -f demo/itoa-serde/Dockerfile -t downstream-detector:demo .
docker run --rm --network none --memory 2g --cpus 2 \
  downstream-detector:demo compare \
  /opt/real-demo/itoa demo-baseline HEAD itoa \
  /opt/real-demo/serde-json HEAD
```

The report compares the baseline and candidate test results. See [the captured run](demo/itoa-serde/report.txt).

## How it uses Agent Substrate

The Rust detector runs as a configured Agent Substrate actor. Substrate schedules it onto a worker; the demo image already contains both repositories and the Cargo cache, so the actor can run the comparison without network access. The full test output and failure backtraces are saved in the report. This prototype tests one configured downstream project per run.

## Video demo

[![Watch the narrated walkthrough](demo/itoa-serde/walkthrough.gif)](demo/itoa-serde/walkthrough.mp4)

[Play or download the 36-second video](demo/itoa-serde/walkthrough.mp4).
