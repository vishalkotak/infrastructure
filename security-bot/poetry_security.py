"""Remediate known Poetry lockfile findings inside a credential-free container."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

def canonical(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Unsupported package name")
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_batches(lock):
    versions = {}
    for package in lock.get("package", []):
        if package.get("source", {}).get("type") in ("git", "directory", "file", "url"):
            raise ValueError("Non-registry Poetry dependencies require a dedicated validator")
        version = str(package["version"])
        if not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
            raise ValueError("Unsupported locked package version")
        versions.setdefault(canonical(package['name']), set()).add(version)
    ordered = {name: sorted(values) for name, values in sorted(versions.items())}
    count = max((len(values) for values in ordered.values()), default=1)
    return ["\n".join(f"{name}=={values[index]}" for name, values in ordered.items()
                       if index < len(values)) + "\n" for index in range(count)]


def requirements(lock):
    batches = requirement_batches(lock)
    if len(batches) != 1:
        raise ValueError("Platform variants require separate audit batches")
    return batches[0]


def write_requirement_files(lock, base):
    base = Path(base)
    for old in base.parent.glob(base.stem + ".*" + base.suffix):
        old.unlink()
    files = []
    for index, content in enumerate(requirement_batches(lock)):
        output = base.with_name(f"{base.stem}.{index}{base.suffix}")
        output.write_text(content)
        files.append(output)
    return files


def audit_lock(lock, base=Path("/tmp/security-locked.txt")):
    data = {"dependencies": []}
    had_findings = False
    for index, requirements_file in enumerate(write_requirement_files(lock, base)):
        output = requirements_file.with_suffix(".audit.json")
        output.unlink(missing_ok=True)
        audit = subprocess.run(["python", "-m", "pip_audit", "-r", str(requirements_file), "--no-deps",
                                "--disable-pip", "-f", "json", "-o", str(output)], check=False)
        if audit.returncode not in (0, 1) or not output.is_file():
            raise RuntimeError("Could not audit frozen Poetry dependency batch " + str(index))
        data["dependencies"].extend(json.loads(output.read_text())["dependencies"])
        had_findings = had_findings or audit.returncode == 1
    return data, had_findings


def patched_versions(audit):
    result = {}
    for dependency in audit.get("dependencies", []):
        minima = []
        for finding in dependency.get("vulns", []):
            stable = [value for value in finding.get("fix_versions", [])
                      if re.fullmatch(r"\d+(?:\.\d+)*", value)]
            if not stable:
                raise ValueError(f"No stable fix for {dependency['name']}: {finding.get('id')}")
            minima.append(min(stable, key=lambda value: tuple(map(int, value.split('.')))))
        if minima:
            name = canonical(dependency["name"])
            if name in result:
                minima.append(result[name])
            result[name] = max(minima, key=lambda value: tuple(map(int, value.split('.'))))
    return result


def direct_dependencies(manifest):
    poetry = manifest.get("tool", {}).get("poetry", {})
    result = {canonical(name): None for name in poetry.get("dependencies", {}) if name != "python"}
    result.update({canonical(name): "dev" for name in poetry.get("dev-dependencies", {})})
    for group, config in poetry.get("group", {}).items():
        if group == "security":
            continue
        result.update({canonical(name): group for name in config.get("dependencies", {})})
    return result


def affected_parents(lock, manifest, vulnerable):
    direct = direct_dependencies(manifest)
    reverse = {}
    for package in lock.get("package", []):
        for dependency in package.get("dependencies", {}):
            reverse.setdefault(canonical(dependency), set()).add(canonical(package["name"]))
    pending = list(vulnerable)
    visited = set()
    parents = {}
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        if name in direct:
            parents[name] = direct[name]
        pending.extend(reverse.get(name, ()))
    return parents


def allowed_manifest_change(before, after):
    """Only dependency tables may change; scripts and build configuration stay fixed."""
    def stripped(manifest):
        value = json.loads(json.dumps(manifest))
        poetry = value.get("tool", {}).get("poetry", {})
        poetry.pop("dependencies", None)
        poetry.pop("dev-dependencies", None)
        groups = poetry.get("group", {})
        for group in list(groups):
            groups[group].pop("dependencies", None)
            if not groups[group]:
                del groups[group]
        if not groups:
            poetry.pop("group", None)
        return value
    if stripped(before) != stripped(after):
        return False
    old = direct_dependencies(before)
    new = direct_dependencies(after)
    return all(name in new and new[name] == group for name, group in old.items())


def runtime_floor_for_error(current, output, runtime_minor):
    match = re.fullmatch(r"\^3\.(\d+)(?:\.\d+)?", str(current))
    if not match:
        match = re.fullmatch(r">=3\.(\d+)(?:\.\d+)?,\s*<4(?:\.0)?", str(current))
    required = [int(value) for value in re.findall(r"requires Python\s+>=\s*3\.(\d+)", output)]
    if not match or not required:
        return None
    floor = max(required)
    if floor <= int(match.group(1)) or floor > runtime_minor:
        return None
    return f">=3.{floor},<4.0"


def command(args, allow_runtime_update=True):
    result = subprocess.run(args, check=False, text=True, capture_output=True)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        if allow_runtime_update:
            path = Path("pyproject.toml")
            metadata = tomllib.loads(path.read_text())
            current = metadata.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
            replacement = runtime_floor_for_error(current, result.stdout + result.stderr, sys.version_info.minor)
            if replacement:
                # Poetry supplies tomlkit in this isolated preparation container.
                import tomlkit
                document = tomlkit.parse(path.read_text())
                document["tool"]["poetry"]["dependencies"]["python"] = replacement
                path.write_text(tomlkit.dumps(document))
                print(f"Security fixes require a newer Python minimum: {current} -> {replacement}", flush=True)
                return command(args, allow_runtime_update=False)
        raise RuntimeError("Dependency solver could not produce a compatible secure lockfile")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", help="Export frozen lockfile versions for audit without installing them")
    args = parser.parse_args()
    lock_path = Path("poetry.lock")
    if args.requirements:
        write_requirement_files(tomllib.loads(lock_path.read_text()), Path(args.requirements))
        return
    for _ in range(3):
        lock = tomllib.loads(lock_path.read_text())
        manifest = tomllib.loads(Path("pyproject.toml").read_text())
        data, had_findings = audit_lock(lock)
        fixes = patched_versions(data)
        if not fixes:
            if had_findings:
                raise RuntimeError("Audit did not confirm a clean lockfile")
            return
        constraints = [f"{name}>={version}" for name, version in sorted(fixes.items())]
        print("Resolving patched dependency constraints: " + ", ".join(constraints), flush=True)
        original_manifest = Path("pyproject.toml").read_bytes()
        original_lock = lock_path.read_bytes()
        try:
            command(["poetry", "add", "--lock", "--no-interaction", "--group", "security", *constraints])
        except RuntimeError:
            Path("pyproject.toml").write_bytes(original_manifest)
            lock_path.write_bytes(original_lock)
            # Existing parent constraints may forbid the patched transitive
            # version. Upgrade only direct ancestors of vulnerable packages.
            parents = affected_parents(lock, manifest, fixes)
            for group in sorted(set(parents.values()), key=lambda x: x or ""):
                specs = [name + "@latest" for name, parent_group in sorted(parents.items()) if parent_group == group]
                args = ["poetry", "add", "--lock", "--no-interaction"]
                if group:
                    args.extend(["--group", group])
                command([*args, *specs])
            command(["poetry", "add", "--lock", "--no-interaction", "--group", "security", *constraints])
        if not allowed_manifest_change(manifest, tomllib.loads(Path("pyproject.toml").read_text())):
            raise RuntimeError("Remediation changed configuration outside dependency tables")
    raise RuntimeError("Poetry remediation did not reach a clean audit within three iterations")


if __name__ == "__main__":
    main()
