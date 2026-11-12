# npm package checks

Give the checker the package pull request as usual:

```sh
downstream-detector check https://github.com/OWNER/REPO/pull/123
```

It detects a changed root or nested `package.json`, reads its static `name`, searches GitHub code for root `package.json` files that mention that package, ranks the sample, and runs each selected consumer's `npm test` against both the PR base and candidate. A consumer needs a root `package.json` with a test script. The runner installs its dependencies from `package-lock.json` (`npm ci`) when present, then installs the checked-out upstream source before running tests.

This first version supports npm projects only: pnpm, Yarn, workspaces, and consumers without `scripts.test` are reported as setup-inconclusive. Discovery requires a read-only GitHub API token and is a best-effort sample of up to 100 code-search results, not a complete reverse dependency index. Use a repository inventory/priority file when you need company-wide coverage.

The actor image includes Node.js and npm. The default egress policy permits `registry.npmjs.org`; add your internal registry host for Artifactory. Registry authentication is not brokered yet, so use an anonymous/read-only route or a separately secured executor. Package lifecycle scripts and tests execute repository-controlled code: keep them inside the isolated actor and do not expose long-lived registry credentials.
