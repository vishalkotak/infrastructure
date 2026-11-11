"""Metadata database."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey, Integer, MetaData, String, Table,
                        Text, UniqueConstraint, create_engine, func, insert, select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .config import settings
from .registry_rules import STAGES, PolicyError, check_artifact, check_metadata, check_name, parse_ref

metadata = MetaData()
ID = BigInteger().with_variant(Integer, "sqlite")

models = Table(
    "models", metadata,
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("name", String(63), nullable=False, unique=True),
    Column("owner", String(128), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

model_versions = Table(
    "model_versions", metadata,
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("model_id", BigInteger, ForeignKey("models.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("artifact_uri", String(512), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("stage", String(16), nullable=False, default="NONE"),
    Column("metrics", Text, nullable=False, default="{}"),
    Column("tags", Text, nullable=False, default="{}"),
    Column("description", Text, nullable=False, default=""),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("stage_updated_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("model_id", "version"),
    UniqueConstraint("model_id", "sha256"),
)


class NotFound(Exception):
    pass


class ConflictError(Exception):
    pass


_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.db_url, pool_pre_ping=True)
    return _engine


def init_db() -> None:
    metadata.create_all(engine())


# ---- models

def _model(row) -> dict:
    return {"name": row.name, "owner": row.owner, "description": row.description,
            "created_at": str(row.created_at)}


def _model_id(conn, name: str) -> int:
    model_id = conn.execute(select(models.c.id).where(models.c.name == name)).scalar_one_or_none()
    if model_id is None:
        raise NotFound(f"model {name}")
    return model_id


def create_model(name: str, owner: str, description: str = "") -> dict:
    check_name(name)
    created_at = datetime.now(timezone.utc)
    with engine().begin() as conn:
        try:
            conn.execute(insert(models).values(name=name, owner=owner, description=description,
                                               created_at=created_at))
        except IntegrityError as e:
            raise ConflictError(f"model {name} already exists") from e
    return {"name": name, "owner": owner, "description": description, "created_at": str(created_at),
            "latest_version": None, "versions": 0}


def get_model(name: str) -> dict:
    mv = model_versions.c
    latest = select(func.max(mv.version)).where(mv.model_id == models.c.id).scalar_subquery()
    count = select(func.count()).where(mv.model_id == models.c.id).scalar_subquery()
    with engine().connect() as conn:
        row = conn.execute(select(models, latest.label("latest_version"), count.label("versions"))
                           .where(models.c.name == name)).one_or_none()
    if row is None:
        raise NotFound(f"model {name}")
    return {**_model(row), "latest_version": row.latest_version, "versions": row.versions}


def list_models(query: str = "") -> list[dict]:
    stmt = select(models).order_by(models.c.name)
    if query:
        stmt = stmt.where(models.c.name.contains(query.lower()))
    with engine().connect() as conn:
        return [_model(r) for r in conn.execute(stmt).all()]


# ---- versions

@dataclass
class ModelVersionRecord:
    id: int
    model: str
    version: int
    artifact_uri: str
    sha256: str
    size_bytes: int
    stage: str
    metrics: dict
    tags: dict
    description: str
    created_by: str
    created_at: str

    @property
    def ref(self) -> str:
        return f"{self.model}@{self.version}"

    def to_dict(self) -> dict:
        return {**asdict(self), "ref": self.ref}


def _version(row, model: str) -> ModelVersionRecord:
    return ModelVersionRecord(
        row.id, model, row.version, row.artifact_uri, row.sha256, row.size_bytes, row.stage,
        json.loads(row.metrics), json.loads(row.tags), row.description, row.created_by, str(row.created_at))


def register_version(model: str, artifact_uri: str, sha256: str, size_bytes: int, created_by: str,
                     metrics: dict | None = None, tags: dict | None = None,
                     description: str = "") -> tuple[ModelVersionRecord, bool]:
    """Returns (version, created). Same bytes again returns the existing version."""
    metrics, tags = metrics or {}, tags or {}
    check_artifact(artifact_uri, sha256, size_bytes)
    check_metadata(metrics, tags)
    mv = model_versions.c
    latest = select(func.max(mv.version)).where(mv.model_id == models.c.id).scalar_subquery()
    existing = select(mv.id).where(mv.model_id == models.c.id, mv.sha256 == sha256).scalar_subquery()

    with engine().begin() as conn:
        m = conn.execute(select(models.c.id, latest.label("latest"), existing.label("existing"))
                         .where(models.c.name == model).with_for_update()).one_or_none()
        if m is None:
            raise NotFound(f"model {model}")
        if m.existing is not None:
            row = conn.execute(select(model_versions).where(mv.id == m.existing)).one()
            return _version(row, model), False

        version = (m.latest or 0) + 1
        created_at = datetime.now(timezone.utc)
        try:
            res = conn.execute(insert(model_versions).values(
                model_id=m.id, version=version, artifact_uri=artifact_uri, sha256=sha256,
                size_bytes=size_bytes, metrics=json.dumps(metrics), tags=json.dumps(tags),
                description=description, created_by=created_by, created_at=created_at))
        except IntegrityError as e:
            raise ConflictError(f"version race on {model}; retry") from e

    record = ModelVersionRecord(res.inserted_primary_key[0], model, version, artifact_uri, sha256, size_bytes,
                                "NONE", metrics, tags, description, created_by, str(created_at))
    return record, True


def _versions_of(model: str):
    return (select(model_versions).join(models, models.c.id == model_versions.c.model_id)
            .where(models.c.name == model).order_by(model_versions.c.version.desc()))


def _find(conn, model: str, *, version: int | None = None, stage: str | None = None):
    stmt = _versions_of(model)
    if version is not None:
        stmt = stmt.where(model_versions.c.version == version)
    if stage is not None:
        stmt = stmt.where(model_versions.c.stage == stage)
    row = conn.execute(stmt.limit(1)).one_or_none()
    if row is None:
        _model_id(conn, model)  # only on a miss: say which of model or version is missing
    return row


def get_version(model: str, version: int) -> ModelVersionRecord:
    with engine().connect() as conn:
        row = _find(conn, model, version=version)
    if row is None:
        raise NotFound(f"{model}@{version}")
    return _version(row, model)


def resolve(ref: str) -> ModelVersionRecord:
    model, sel = parse_ref(ref)
    with engine().connect() as conn:
        if sel.isdigit():
            row = _find(conn, model, version=int(sel))
        elif sel == "latest":
            row = _find(conn, model)
        else:
            row = _find(conn, model, stage=sel.upper())
    if row is None:
        raise NotFound(f"no version matches {model}@{sel}")
    return _version(row, model)


def list_versions(model: str, stage: str | None = None, tag: str | None = None,
                  limit: int | None = None) -> list[ModelVersionRecord]:
    """Newest first. tag is 'key' or 'key=value'."""
    if stage is not None and stage.upper() not in STAGES:
        raise PolicyError(f"unknown stage {stage}; stages are {', '.join(STAGES)}")
    stmt = _versions_of(model)
    if stage:
        stmt = stmt.where(model_versions.c.stage == stage.upper())
    if limit and not tag:
        stmt = stmt.limit(limit)
    with engine().connect() as conn:
        rows = conn.execute(stmt).all()
        if not rows:
            _model_id(conn, model)
    out = [_version(r, model) for r in rows]
    if tag:
        key, _, value = tag.partition("=")
        out = [v for v in out if key in v.tags and (not value or v.tags[key] == value)]
    return out[:limit] if limit else out