# Model Store

Model registry and Kubernetes deployment control plane, built to run on a laptop.

The host-side control plane runs natively on macOS (arm64) and talks to local Kubernetes clusters (minikube), Postgres (or TiDB), S3-compatible storage (MinIO) and Prometheus.

## What's built

- **Registry** (`modelstore/`): models, versions, stage lifecycle (staging, production, archived), tags. Postgres or TiDB.
- **Artifacts**: S3/MinIO, sha256-verified on fetch.
- **`ModelEndpoint` CRD + kopf operator** (`dataplane/`, `deploy/k8s/`): reconciles stable and canary Deployments with surge-first rollouts.
- **Model server**: FastAPI, warmup before readiness, batched predict, Prometheus metrics.

Next: Temporal deploy workflows, automated canary analysis, GPU placement.

## Setup

Requires Python 3.11+ and [Poetry](https://python-poetry.org/).

```sh
poetry install                  # core + dev dependencies
poetry install --with demo      # also install torch, to build demo model artifacts
poetry install --extras tidb    # use TiDB instead of Postgres
```

## Configuration

Configuration is read from `MS_*` environment variables. Copy the example file and source it:

```sh
cp .env.example .env
source .env
```

`.env.example` lists every variable with its default value.

## Usage

```sh
poetry run ms --help
```

## Tests

```sh
poetry run pytest
```
