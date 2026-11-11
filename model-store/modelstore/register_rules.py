"""Registry rules for references, stage changes and deploys."""
from __future__ import annotations

import re


class PolicyError(Exception):
    pass


STAGES = ("NONE", "STAGING", "PRODUCTION", "ARCHIVED")

TRANSITIONS = {
    "NONE": {"STAGING", "ARCHIVED"},
    "STAGING": {"PRODUCTION", "ARCHIVED"},
    "PRODUCTION": {"STAGING", "ARCHIVED"},
    "ARCHIVED": {"STAGING"},
}

_REF = re.compile(r"^(?P<model>[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)@(?P<sel>\d+|latest|staging|production)$")


def validate_deployable_stages(deployable: tuple[str, ...]) -> tuple[str, ...]:
    stages = tuple(s.strip().upper() for s in deployable if s.strip())
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise PolicyError(f"unknown stage(s) in deployable stages: {', '.join(unknown)}")
    missing = [s for s in ("STAGING", "PRODUCTION") if s not in stages]
    if missing:
        raise PolicyError(f"deployable stages must include {', '.join(missing)}")
    if "ARCHIVED" in stages:
        raise PolicyError("ARCHIVED cannot be deployable")
    return stages


def parse_ref(ref: str) -> tuple[str, str]:
    """'model@3' -> ('model', '3')."""
    m = _REF.match(ref.strip().lower())
    if not m:
        raise PolicyError(f"bad model reference {ref!r}; use <model>@<version|latest|staging|production>")
    sel = m["sel"]
    if sel.isdigit():
        if int(sel) < 1:
            raise PolicyError(f"bad model reference {ref!r}; versions start at 1")
        sel = str(int(sel))
    return m["model"], sel


def check_transition(current: str, target: str) -> None:
    target = target.upper()
    if target not in STAGES:
        raise PolicyError(f"unknown stage {target}; stages are {', '.join(STAGES)}")
    if target != current and target not in TRANSITIONS[current]:
        allowed = ", ".join(sorted(TRANSITIONS[current]))
        raise PolicyError(f"cannot move a version from {current} to {target}; from {current} you can go to {allowed}")


def check_stage_change(current: str, target: str, *, serving_on: list[str], deployed_on: list[str],
                       deployable: tuple[str, ...], force: bool = False) -> None:
    target = target.upper()
    check_transition(current, target)  # force never skips this
    if target == current or force:
        return
    if target == "PRODUCTION" and not deployed_on:
        where = f" (rollout still in progress on {', '.join(sorted(serving_on))})" if serving_on else ""
        raise PolicyError(f"a version must complete a rollout before it can be PRODUCTION{where}")
    if serving_on and target not in deployable:
        raise PolicyError(f"version is still serving on {', '.join(sorted(serving_on))} "
                          f"and {target} is not deployable; use force to override")


def displaced_stage(serving_on: list[str], archive_existing: bool = True) -> str:
    """Stage for the previous PRODUCTION version on promotion."""
    if serving_on or not archive_existing:
        return "STAGING"
    return "ARCHIVED"


def check_deploy(kind: str, *, endpoint_model: str, version_model: str, version_ref: str, stage: str,
                 deployable: tuple[str, ...], already_serving_here: bool) -> None:
    kind = kind.upper()
    if kind not in ("DEPLOY", "ROLLBACK"):
        raise PolicyError(f"unknown deploy kind {kind}")
    if version_model != endpoint_model:
        raise PolicyError(f"this endpoint serves model {endpoint_model}; {version_ref} belongs to {version_model}")
    if kind == "ROLLBACK":
        return  # rollbacks ignore stage
    if already_serving_here:
        raise PolicyError(f"{version_ref} is already serving on this endpoint")
    if stage not in deployable:
        hint = {"NONE": "; stage it first", "ARCHIVED": "; re-qualify it through STAGING first"}.get(stage, "")
        raise PolicyError(f"{version_ref} is in stage {stage}; deployable stages are {', '.join(deployable)}{hint}")