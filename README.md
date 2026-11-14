# infrastructure

| Project | |
|---|---|
| [downstream-detector](downstream-detector/) | Runs downstream consumers' tests against a library PR to catch breaks the API doesn't show. |
| [model-store](model-store/) | Model registry and deployment control plane: FastAPI, Temporal, Postgres/TiDB, MinIO, k8s. |
| [security-bot](security-bot/) | Daily job that patches vulnerable dependencies across my repos, tests the exact merge commit, then pushes. |
