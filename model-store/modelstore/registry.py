from __future__ import annotations

from . import artifacts, db
from .registry_rules import check_metadata


def publish(model: str, path: str, created_by: str, metrics: dict | None = None,
            tags: dict | None = None, description: str = "") -> tuple[db.ModelVersionRecord, bool]:
    db.get_model(model)  # fail before uploading anything
    check_metadata(metrics or {}, tags or {})
    uri, sha, size = artifacts.upload(model, path)
    return db.register_version(model, uri, sha, size, created_by, metrics, tags, description)