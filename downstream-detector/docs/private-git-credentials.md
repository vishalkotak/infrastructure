# Private Git access from Substrate actors

**Yes.** Private Git access needs one-time setup by someone who can configure the Substrate cluster's egress credential provider and Kubernetes secrets. Public GitHub repositories do not need this setup. It applies to the built-in `substrate` executor; a command executor must arrange credentials through its own secret manager.

There are two separate credentials:

| Credential | Used by | Purpose |
| --- | --- | --- |
| `DETECTOR_GHES_TOKEN` | CLI/controller | Read PR metadata and search repositories through the GitHub Enterprise API. |
| Brokered Git token | Substrate egress gateway | Clone the upstream and selected private downstream repositories inside an actor. The actor and its test code never receive the token value. |

Do not reuse the controller token as the brokered Git token. The detector requests a placeholder `Authorization` header for the configured Git host; the gateway replaces it with the brokered credential. The actor's HTTPS trust bundle is mounted automatically when `DETECTOR_GIT_AUTH_HOST` is configured.

## One-time cluster setup

These steps assume the cluster has the Substrate Kubernetes Secrets credential provider installed and enabled. If it does not, a cluster administrator must install and configure a provider first; follow Substrate's [egress credential injection guide](https://ate.dev/docs/guides/egress-credential-injection/). The guide also describes the provider's atespace-to-namespace authorization policy.

1. **Choose the Git host and repository scope.** Create a GitHub fine-grained token or GitHub App installation token with contents read access only. Restrict it to the upstream crate repository and the private consumer repositories this run may check. The detector currently uses one credential for all selected repositories in an actor run; it does not mint repository-specific GitHub App tokens.

2. **Store the token in a Kubernetes Secret.** Use the namespace your credential provider is authorized to read. For example:

   ```sh
   umask 077
   token_file=$(mktemp)
   trap 'rm -f "$token_file"' EXIT
   printf '%s' "$DOWNSTREAM_GIT_TOKEN" > "$token_file"
   kubectl -n detector-secrets create secret generic downstream-git \
     --from-file=token="$token_file"
   ```

   Create it through your organization's secret-management process in production. The sample Kubernetes provider reads a stored Secret; it does not mint or rotate GitHub tokens. Arrange token expiry and rotation yourself, or use a provider that can mint short-lived GitHub App installation tokens. Make sure the token remains valid for the full job duration, or that the broker can refresh it while the actor runs.

3. **Authorize the detector atespace to resolve that namespace.** In the credential provider's namespace policy, allow the atespace used by this detector to read only the chosen secret namespace. For example, the provider's ConfigMap policy has this shape:

   ```yaml
   policies:
     - atespace: compatibility
       allowedNamespaces:
         - detector-secrets
   ```

   The exact ConfigMap name and deployment namespace depend on the cluster installation. The reference provider loads its policy at startup; follow the official guide to apply it and restart the provider.

4. **Copy and edit the egress policy.** Start from [`substrate/enterprise-egress.example.yaml`](../substrate/enterprise-egress.example.yaml):

   ```sh
   cp substrate/enterprise-egress.example.yaml substrate/enterprise-egress.yaml
   ```

   Replace `github.example.com`, `REPLACE_NAMESPACE`, and `REPLACE_SECRET` with the Git hostname and Secret details. The final credential URI should have the form `ate-secret://k8s.io/default/<secret-namespace>/<secret-name>/<key>`. Keep the `https` rule: it lets the gateway inspect the request and replace the placeholder header. The detector mounts Substrate's MITM and system-root trust bundles for Git TLS.

5. **Verify cluster authorization and policy before a PR run.** Ensure the Kubernetes credential provider is ready and its atespace policy is active. Check that the policy uses the intended hostname and Secret URI, then run a private-repository comparison with a small job budget. The gateway denies requests if the provider is missing, the Secret cannot be read, or the atespace is not authorized.

## Configure the CLI

Set these in the environment where you run the detector:

```sh
export DETECTOR_GITHUB_HOST=github.example.com
export DETECTOR_GITHUB_ORG=your-company
export DETECTOR_GHES_TOKEN # load this variable from your secret manager before launching
export DETECTOR_GIT_AUTH_HOST=github.example.com
export DETECTOR_EGRESS_POLICY="$PWD/substrate/enterprise-egress.yaml"
export DETECTOR_ATESPACE=compatibility
```

`DETECTOR_GITHUB_HOST` selects the Enterprise API and the API token. `DETECTOR_GITHUB_ORG` enables company repository discovery. `DETECTOR_GHES_TOKEN` is the controller's API token; load it from your secret manager, not a literal in a checked-in shell profile or command. `DETECTOR_GIT_AUTH_HOST` selects the host whose Git requests receive the placeholder header. `DETECTOR_EGRESS_POLICY` is the policy file the CLI submits for each actor. Keep `DETECTOR_GIT_AUTH_HOST` equal to the hostname in that policy. For private repositories on GitHub.com, use `GITHUB_TOKEN` for controller API access and set `DETECTOR_GIT_AUTH_HOST=github.com` with an egress policy for `github.com`.

The Kubernetes Secret provider returns the same configured token to each actor that is authorized to resolve it. Scope that token to the set of repositories the run may access; the current host-level egress policy does not enforce Git repository paths. For large or changing private consumer sets, use a credential broker that mints a short-lived GitHub App token for the specific repositories selected for that run. The CLI's default budget is five jobs, configurable with `DETECTOR_MAX_JOBS` up to 32.

## What you need to do

- **Public repositories only:** no brokered Git credential setup. Substrate still needs an egress policy that permits the public Git and Cargo hosts used by your job.
- **Private repositories:** ask the Substrate cluster administrator to configure the provider and namespace authorization once; ask a GitHub administrator to create the read-only token; then set the CLI variables above. The detector does not create Kubernetes secrets, mint GitHub tokens, or configure cluster-wide credential providers.

For the underlying mechanics and operational details, see the official [credential injection guide](https://ate.dev/docs/guides/egress-credential-injection/) and [MITM trust bundle guide](https://ate.dev/docs/guides/egress-trust-bundle/).
