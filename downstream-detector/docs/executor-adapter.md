# Executor adapter protocol

The PR checker owns GitHub intake, consumer discovery and ranking, job budgets, and result reports. An executor adapter owns where a compatibility check runs. The built-in `substrate` executor is one implementation; a trusted command adapter can submit jobs to Kubernetes, a CI service, a cloud batch system, or another isolated runner.

## Configure a command adapter

Install an executable wrapper on the machine running `downstream-detector`, then configure:

```sh
export DETECTOR_EXECUTOR=command
export DETECTOR_EXECUTOR_COMMAND=/absolute/path/to/downstream-detector-runner
```

The normal PR command remains unchanged:

```sh
downstream-detector check https://github.com/OWNER/REPO/pull/123
```

The detector starts one adapter process per selected consumer, up to `DETECTOR_MAX_PARALLEL`, and writes one JSON request to the process's standard input. The adapter must return one JSON object on standard output. Keep diagnostic output on standard error.

## Request

The protocol is versioned and currently sends:

```json
{
  "protocol_version": 1,
  "operation": "compatibility_check",
  "upstream": "https://github.com/OWNER/CRATE/.git",
  "baseline": "base-commit-sha",
  "candidate": "refs/pull/123/head",
  "crate_name": "crate-name",
  "downstream": "https://github.com/CONSUMER/APP.git",
  "downstream_ref": "consumer-commit-sha",
  "patch_source": null,
  "cargo_offline": false,
  "resources": {"cpu_millicores": 1000, "memory_bytes": 1073741824}
}
```

The adapter should run the existing detector comparison for the request in a fresh, isolated environment, for example by invoking its `compare` command with the upstream, baseline, candidate, crate name, downstream, and downstream revision fields. It should apply the requested resource limits and its own network and secret policies. Do not make repository code or Cargo build scripts share the controller's credentials or control-plane access.

## Response

On completion, write a single JSON object to standard output:

```json
{"status":"completed","executor":"my-runner","report":"Verdict: NO_REGRESSION_DETECTED\\n... full test logs ..."}
```

The `report` string is stored with the PR report and used to produce the CLI summary. For an execution failure, exit nonzero and write diagnostics to standard error. Invalid JSON, a nonzero exit, or a response without `status: completed` and a string `report` is recorded as an executor error.

The adapter command is trusted infrastructure code: it runs on the controller and inherits its environment. Keep untrusted dependency code inside the isolated environment the adapter creates. The payload contains no GitHub token or other secret; the adapter should obtain any needed credentials from its own secret manager and expose only narrowly scoped credentials to the job.
