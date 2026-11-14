# Design

## Problem

Get a model version from training output to serving traffic, with a way back, and without trusting whoever uploaded it.

## Decisions

**Registry rules are code, separate from storage.** `registry_rules.py` owns stage transitions (`NONE`, `STAGING`, `PRODUCTION`, `ARCHIVED`) and reference parsing. `db.py` only persists. Rules are testable without a database, and Postgres and TiDB share them.

**Artifacts are content-addressed.** Every version records sha256 and size. The model server re-verifies the checksum on fetch and never reports ready on a mismatch. A bucket edit can't silently change what serves.

**Kubernetes owns rollout, not the control plane.** A `ModelEndpoint` CRD declares a stable and an optional canary version with replica counts. A kopf operator reconciles that into one Deployment and Service per role. Desired state lives in the cluster, so a control plane crash doesn't strand a rollout.

**Readiness means warm.** The server runs warmup passes before reporting ready. Rollouts are surge first (`maxSurge 1`, `maxUnavailable 0`), so capacity never dips and cold pods never take traffic.

**Metrics at the server.** Prometheus counters and latency histograms per role, so stable and canary can be compared directly.

## Not built yet

Temporal workflows for multi-step deploys, automated canary analysis on those metrics, and GPU placement across clusters. The CRD and per-role metrics are the hooks they plug into.

## At 100x

Registry reads move behind a cache. Artifacts get pre-pulled to nodes before rollout. Canary promotion becomes a Temporal workflow gated on SLO comparison, with automatic rollback.
