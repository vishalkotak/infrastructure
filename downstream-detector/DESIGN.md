# Design

## Problem

A library PR can keep its API and still break consumers. Its own tests can't see that. The only real signal is the consumers' tests.

## Decisions

**Baseline vs candidate, not candidate alone.** Each consumer runs twice: against the PR's base and its head. A consumer that already fails on base is `INCONCLUSIVE_BASELINE_FAILED`, not a regression. Without this, every flaky downstream suite becomes noise blamed on the PR.

**Four outcomes, not pass/fail.** `NO_REGRESSION_DETECTED`, `POTENTIAL_REGRESSION` (tests ran and failed), and two inconclusive states for build or setup failures. A compile error in an unrelated toolchain is not the same claim as a failing test, and the report keeps them apart.

**Ranked sample under a budget.** Popular crates have thousands of dependents. Consumers are deduplicated by repo and ranked (download counts, or a company priority list), then checked 5 at a time by default. This trades coverage for a bounded cost per PR. The report says so.

**Executor is a protocol.** The checker owns intake, discovery, ranking and reporting. Where a job runs is a versioned JSON-over-stdin adapter: Substrate actors, Kubernetes, CI, anything. Swapping runners doesn't touch the detector.

**Untrusted code assumptions.** Consumer tests are arbitrary code. Jobs run isolated with egress limits. The actor server accepts no code, URLs or commands from requests, only its operator config. Child output is drained but capped so a noisy suite can't exhaust memory, and the whole process group is killed on timeout.

## Failure modes

- Python and npm discovery use code search samples, so consumers can be missed. Rust uses crates.io reverse deps and is complete up to the budget.
- A real break in a consumer outside the ranked sample goes unreported.
- Flaky consumer tests can still flip on candidate. Re-running failures before reporting is the next fix.

## At 100x

Cache baseline results per consumer commit so each PR runs only the candidate. Pre-build consumer dependency layers. Shard by ecosystem. Track a per-consumer flake rate and weight the report by it.
