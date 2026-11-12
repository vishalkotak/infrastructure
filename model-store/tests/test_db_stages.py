import pytest

pytest.importorskip("sqlalchemy")

from conftest import sha, uri  # noqa: E402

from modelstore.registry_rules import PolicyError  # noqa: E402


@pytest.fixture
def db(fresh_db):
    fresh_db.create_model("fraud-model", "risk-team")
    for n in (1, 2, 3):
        fresh_db.register_version("fraud-model", uri("fraud-model", n), sha(n), 100, "vishal")
    return fresh_db


def stage(db, v):
    return db.get_version("fraud-model", v).stage


# Nothing is serving yet (no deployments), so promotion needs force.

def test_stage_forward(db):
    rec, displaced = db.set_stage("fraud-model", 1, "staging")
    assert rec.stage == "STAGING" and displaced is None and stage(db, 1) == "STAGING"


def test_lifecycle_refusals(db):
    with pytest.raises(PolicyError):
        db.set_stage("fraud-model", 1, "production", force=True)   # NONE -> PRODUCTION
    db.set_stage("fraud-model", 1, "staging")
    with pytest.raises(PolicyError):
        db.set_stage("fraud-model", 1, "none")


def test_promote_needs_completed_rollout(db):
    db.set_stage("fraud-model", 1, "staging")
    with pytest.raises(PolicyError, match="complete a rollout"):
        db.set_stage("fraud-model", 1, "production")
    assert stage(db, 1) == "STAGING"


def test_promote_displaces_previous(db):
    for v in (1, 2):
        db.set_stage("fraud-model", v, "staging")
    db.set_stage("fraud-model", 1, "production", force=True)
    rec, displaced = db.set_stage("fraud-model", 2, "production", force=True)
    assert rec.stage == "PRODUCTION"
    assert displaced == {"ref": "fraud-model@1", "from": "PRODUCTION", "to": "ARCHIVED"}
    assert stage(db, 1) == "ARCHIVED" and stage(db, 2) == "PRODUCTION"
    assert [v.version for v in db.list_versions("fraud-model", stage="production")] == [2]


def test_keep_displaced_in_staging(db):
    for v in (1, 2):
        db.set_stage("fraud-model", v, "staging")
    db.set_stage("fraud-model", 1, "production", force=True)
    _, displaced = db.set_stage("fraud-model", 2, "production", force=True, archive_existing=False)
    assert displaced["to"] == "STAGING" and stage(db, 1) == "STAGING"


def test_expect_production(db):
    for v in (1, 2, 3):
        db.set_stage("fraud-model", v, "staging")
    db.set_stage("fraud-model", 1, "production", expect_production=0, force=True)
    db.set_stage("fraud-model", 2, "production", expect_production=1, force=True)       # Alice
    with pytest.raises(db.ConflictError, match="production is now fraud-model@2, not fraud-model@1"):
        db.set_stage("fraud-model", 3, "production", expect_production=1, force=True)   # Bob, stale
    assert stage(db, 3) == "STAGING" and stage(db, 2) == "PRODUCTION"
    db.set_stage("fraud-model", 3, "production", expect_production=2, force=True)       # Bob, re-read
    assert stage(db, 3) == "PRODUCTION"


def test_expect_none(db):
    db.set_stage("fraud-model", 1, "staging")
    db.set_stage("fraud-model", 1, "production", force=True)
    with pytest.raises(db.ConflictError, match="not none"):
        db.set_stage("fraud-model", 2, "staging", expect_production=0)


def test_demote_and_noop(db):
    db.set_stage("fraud-model", 1, "staging")
    db.set_stage("fraud-model", 1, "production", force=True)
    rec, _ = db.set_stage("fraud-model", 1, "staging")
    assert rec.stage == "STAGING"
    with pytest.raises(db.NotFound):
        db.resolve("fraud-model@production")
    rec, displaced = db.set_stage("fraud-model", 1, "staging")
    assert rec.stage == "STAGING" and displaced is None


def test_stage_not_found(db):
    with pytest.raises(db.NotFound):
        db.set_stage("fraud-model", 9, "staging")
    with pytest.raises(db.NotFound):
        db.set_stage("nope", 1, "staging")


def test_tags(db):
    assert db.set_tags("fraud-model", 1, add={"dataset": "2026-09"}).tags == {"dataset": "2026-09"}
    assert db.set_tags("fraud-model", 1, add={"dataset": "2026-10", "gpu": "h100"}).tags == \
        {"dataset": "2026-10", "gpu": "h100"}
    assert db.set_tags("fraud-model", 1, remove=["gpu", "missing"]).tags == {"dataset": "2026-10"}
    assert db.get_version("fraud-model", 1).tags == {"dataset": "2026-10"}


def test_tag_refusals(db):
    with pytest.raises(PolicyError):
        db.set_tags("fraud-model", 1, add={"x": "1"}, remove=["x"])
    with pytest.raises(db.NotFound, match="fraud-model@9"):
        db.set_tags("fraud-model", 9, add={"x": "1"})
    with pytest.raises(db.NotFound, match="model nope"):
        db.set_tags("nope", 1, add={"x": "1"})