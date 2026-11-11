from __future__ import annotations
from dataclasses import asdict, dataclass
import datetime
import json

from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, func, insert, select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .config import settings
from .registry_rules import check_artifact, check_metadata, check_name

# Registry of table definitions; nothing touches the DB until create_all().
metadata = MetaData()
# BIGINT everywhere, INTEGER on SQLite: only "INTEGER PRIMARY KEY" auto-increments there.
ID = BigInteger().with_variant(Integer, "sqlite")

# Defines and registers the table in `metadata` at import time; does not create it.
models = Table(
    "models", metadata,
    Column("id", ID, primary_key=True, autoincrement=True),
    Column("name", String(63), nullable=False, unique=True),  # DB enforces uniqueness, race-safe
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

class NotFound(Exception):
    pass


class ConflictError(Exception):
    pass

_engine: Engine | None = None

def engine() -> Engine:
    """Shared engine, built lazily on first use. Building it doesn't connect."""
    global _engine
    if _engine is None:
        # pre_ping swaps out pooled connections that died (e.g. after a DB restart).
        _engine = create_engine(settings.db_url, pool_pre_ping=True)
    return _engine


def init_db() -> None:
    """Create missing tables; first real DB connection. Safe to rerun; never alters existing tables."""
    metadata.create_all(engine())


def _model(row) -> dict:
    return {"name": row.name, "owner": row.owner, "description": row.description,
            "created_at": str(row.created_at)}


def get_model(name: str) -> dict:
    with engine().connect() as conn:
        row = conn.execute(select(models).where(models.c.name == name)).one_or_none()
    if row is None:
        raise NotFound(f"model {name}")
    return _model(row)


def create_model(name: str, owner: str, description: str = "") -> dict:
    check_name(name)
    with engine().begin() as conn:
        try:
            conn.execute(insert(models).values(name=name, owner=owner, description=description))
        except IntegrityError as e:
            raise ConflictError(f"model {name} already exists") from e
    return get_model(name)


def list_models(query: str = "") -> list[dict]:
    stmt = select(models).order_by(models.c.name)
    if query:
        stmt = stmt.where(models.c.name.contains(query.lower()))
    with engine().connect() as conn:
        return [_model(r) for r in conn.execute(stmt).all()]


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
        created_at = datetime.datetime.now(datetime.timezone.utc)
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