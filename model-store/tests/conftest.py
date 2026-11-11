import pytest


@pytest.fixture
def fresh_db(tmp_path):
    """A throwaway SQLite database per test."""
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import create_engine

    from modelstore import db
    db._engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db()
    yield db
    db._engine.dispose()
    db._engine = None


def sha(n: int) -> str:
    return f"{n:064x}"


def uri(model: str, n: int) -> str:
    return f"s3://models/{model}/{sha(n)}"