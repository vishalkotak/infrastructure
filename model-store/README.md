# Model Store

A model registry, deployment control plane and GPU placement layer, built to run on a laptop.

The host-side control plane runs natively on macOS (arm64) and talks to local Kubernetes clusters (minikube), Temporal, Postgres (or TiDB), S3-compatible storage (MinIO) and Prometheus.

## Layout

| Path | Purpose |
| --- | --- |
| `modelstore/api` | FastAPI control-plane service |
| `modelstore/workflows` | Temporal workflows for deployments |
| `modelstore/canary` | Canary analysis and promotion |
| `modelstore/placement` | GPU placement across data centres |
| `modelstore/cli.py` | `ms` command-line tool |
| `dataplane/` | Data-plane components |
| `deploy/k8s/` | Kubernetes manifests |
| `scripts/` | Helper scripts |
| `tests/` | Test suite |

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
