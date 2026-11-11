import pytest

pytest.importorskip("sqlalchemy")

from conftest import sha, uri  # noqa: E402

from modelstore.registry_rules import PolicyError  # noqa: E402


@pytest.fixture
def db(fresh_db):
    fresh_db.create_model("fraud-model", "risk-team")
    return fresh_db


def reg(db, n, model="fraud-model", **kw):
    return db.register_version(model, uri(model, n), sha(n), 100 + n, "vishal", **kw)


# ---- register

def test_numbers_increase(db):
    assert [reg(db, n)[0].version for n in (1, 2, 3)] == [1, 2, 3]
    m = db.get_model("fraud-model")
    assert m["latest_version"] == 3 and m["versions"] == 3


def test_new_version_fields(db):
    v, created = reg(db, 1, metrics={"auc": 0.91}, tags={"dataset": "2026-10"}, description="first")
    assert created and v.ref == "fraud-model@1" and v.stage == "NONE"
    assert v.metrics == {"auc": 0.91} and v.tags == {"dataset": "2026-10"} and v.size_bytes == 101
    stored = db.get_version("fraud-model", 1).to_dict()
    assert {k: x for k, x in stored.items() if k != "created_at"} == \
        {k: x for k, x in v.to_dict().items() if k != "created_at"}


def test_same_bytes_return_existing(db):
    first, _ = reg(db, 1)
    again, created = reg(db, 1, metrics={"auc": 0.5})
    assert not created and again.version == first.version and again.metrics == {}
    assert db.get_model("fraud-model")["versions"] == 1


def test_same_bytes_other_model_is_separate(db):
    db.create_model("fraud-lite", "a")
    reg(db, 1)
    v, created = db.register_version("fraud-lite", uri("fraud-lite", 1), sha(1), 101, "vishal")
    assert created and v.ref == "fraud-lite@1"


def test_numbering_is_per_model(db):
    db.create_model("churn-model", "a")
    reg(db, 1)
    reg(db, 2)
    assert reg(db, 3, model="churn-model")[0].version == 1


def test_register_refusals(db):
    with pytest.raises(db.NotFound):
        reg(db, 1, model="nope")
    with pytest.raises(PolicyError):
        reg(db, 1, metrics={"auc": "high"})
    with pytest.raises(PolicyError):
        db.register_version("fraud-model", "/tmp/x.pt", sha(1), 10, "vishal")
    assert db.get_model("fraud-model")["versions"] == 0


# ---- read

def test_get_version(db):
    reg(db, 1)
    assert db.get_version("fraud-model", 1).ref == "fraud-model@1"
    with pytest.raises(db.NotFound, match="fraud-model@9"):
        db.get_version("fraud-model", 9)
    with pytest.raises(db.NotFound, match="model nope"):
        db.get_version("nope", 1)


def test_resolve(db):
    for n in (1, 2, 3, 4):
        reg(db, n)
    db.set_stage("fraud-model", 2, "staging")
    db.set_stage("fraud-model", 2, "production", force=True)
    db.set_stage("fraud-model", 3, "staging")
    assert db.resolve("fraud-model@2").version == 2
    assert db.resolve("fraud-model@002").version == 2
    assert db.resolve("fraud-model@latest").version == 4
    assert db.resolve("fraud-model@production").version == 2
    assert db.resolve("Fraud-Model@Staging").version == 3
    with pytest.raises(db.NotFound, match="no version matches"):
        db.resolve("fraud-model@9")
    with pytest.raises(db.NotFound, match="model nope"):
        db.resolve("nope@1")
    with pytest.raises(PolicyError):
        db.resolve("fraud-model")


def test_list_versions(db):
    reg(db, 1, tags={"dataset": "2026-09"})
    reg(db, 2, tags={"dataset": "2026-10"})
    reg(db, 3, tags={"dataset": "2026-10", "gpu": "h100"})
    refs = lambda vs: [v.version for v in vs]  # noqa: E731
    assert refs(db.list_versions("fraud-model")) == [3, 2, 1]
    assert refs(db.list_versions("fraud-model", tag="dataset=2026-10")) == [3, 2]
    assert refs(db.list_versions("fraud-model", tag="gpu")) == [3]
    assert refs(db.list_versions("fraud-model", tag="dataset=2026-10", limit=1)) == [3]
    assert refs(db.list_versions("fraud-model", limit=2)) == [3, 2]
    assert refs(db.list_versions("fraud-model", stage="none")) == [3, 2, 1]
    assert db.list_versions("fraud-model", stage="production") == []


def test_list_versions_edges(db):
    assert db.list_versions("fraud-model") == []
    with pytest.raises(db.NotFound):
        db.list_versions("nope")
    with pytest.raises(PolicyError, match="unknown stage prod"):
        db.list_versions("fraud-model", stage="prod")