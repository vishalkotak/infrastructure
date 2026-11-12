# Python package checks

Pass the same pull request URL used for Rust. The checker identifies a Python package from static metadata in `pyproject.toml` (`[project]` or `[tool.poetry]`), `setup.cfg` (`[metadata]`), or a literal `name=` in `setup.py`.

```sh
downstream-detector check https://github.com/OWNER/REPO/pull/123
```

Python support currently targets repositories with these common manifests and tests runnable by `pytest` or `unittest`. Each baseline and candidate gets a fresh Python virtual environment. The runner installs recognized downstream requirements files, installs the downstream project when it has packaging metadata, installs the checked-out upstream package source, then runs `python -m pytest -q` when pytest is available or `python -m unittest discover -v` otherwise. A build/setup failure is reported as inconclusive, not as a regression.

The built-in detector image includes Python 3, pip, setuptools, and pytest. Build and use a new digest-pinned image after updating the Dockerfile. The default egress policy allows PyPI's `pypi.org` and `files.pythonhosted.org` hosts. If you use an internal package index such as Artifactory, add its host to the actor's egress policy and configure pip for that index. Authenticated index injection is not yet wired into the detector, so do not bake a long-lived Artifactory credential into the image or expose one to project builds/tests; use an anonymous/read-only network path or a separately secured executor until brokered pip credentials are supported.

## Consumer discovery limits

There is no complete Python reverse-dependency source in this prototype. It searches GitHub code for the package name in `pyproject.toml`, `requirements.txt`, `setup.cfg`, and `setup.py`, optionally including `DETECTOR_GITHUB_ORG`. A read-only GitHub API token is required (`GITHUB_TOKEN` on GitHub.com or `DETECTOR_GHES_TOKEN` for GHES). It uses up to the first 100 matches per manifest, ranks the search results by relevance unless `DETECTOR_PRIORITY_FILE` supplies a repository priority, and checks only the configured job budget.

This is a useful way to find candidate consumers, but it is a sample: it can miss repositories, return a match that is not an installed dependency, and does not cover private repositories the token cannot search. On GitHub Enterprise Server it searches the configured host. Reports label Python discovery as a GitHub code-search sample rather than complete coverage. Use your own repository inventory/priority file when the set of consumers matters.

Run checks in an isolated executor. `pip install`, package build backends, and downstream tests execute repository-controlled code. The built-in Substrate executor provides the sandbox; custom adapters must provide equivalent isolation and network/credential controls. See [private Git setup](private-git-credentials.md) for brokered private Git access.
