from __future__ import annotations

from sqlalchemy import (BigInteger, Column, DateTime, Integer, MetaData, String, Table, Text,
                        create_engine, func, insert, select)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from .config import settings
from .registry_rules import check_name

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