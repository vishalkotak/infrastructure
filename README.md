# infrastructure

| Project | |
|---|---|
| [downstream-detector](downstream-detector/) | Runs downstream consumers' tests against a library PR to catch breaks the API doesn't show. |
| [model-store](model-store/) | Model registry plus a k8s operator for stable/canary model rollouts. |
| [security-bot](security-bot/) | Daily job that patches vulnerable dependencies across my repos, tests the exact merge commit, then pushes. |
