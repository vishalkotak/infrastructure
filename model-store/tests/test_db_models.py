import pytest

pytest.importorskip("sqlalchemy")

from modelstore.registry_rules import PolicyError  # noqa: E402


def test_create_and_get(fresh_db):
    db = fresh_db
    m = db.create_model("fraud-model", "risk-team", "Card fraud scorer")
    assert m["name"] == "fraud-model" and m["versions"] == 0 and m["latest_version"] is None
    got = db.get_model("fraud-model")
    assert got["owner"] == "risk-team" and got["description"] == "Card fraud scorer"


def test_duplicate_name(fresh_db):
    fresh_db.create_model("fraud-model", "a")
    with pytest.raises(fresh_db.ConflictError):
        fresh_db.create_model("fraud-model", "b")


def test_bad_name_never_reaches_db(fresh_db):
    with pytest.raises(PolicyError):
        fresh_db.create_model("Fraud_Model", "a")
    assert fresh_db.list_models() == []


def test_missing(fresh_db):
    with pytest.raises(fresh_db.NotFound):
        fresh_db.get_model("nope")
    with pytest.raises(fresh_db.NotFound):
        fresh_db.check_model_exists("nope")
    fresh_db.create_model("fraud-model", "a")
    fresh_db.check_model_exists("fraud-model")


def test_list_and_search(fresh_db):
    for name in ("fraud-model", "churn-model", "fraud-lite"):
        fresh_db.create_model(name, "a")
    assert [m["name"] for m in fresh_db.list_models()] == ["churn-model", "fraud-lite", "fraud-model"]
    assert [m["name"] for m in fresh_db.list_models("fraud")] == ["fraud-lite", "fraud-model"]
    assert fresh_db.list_models("FRAUD") == fresh_db.list_models("fraud")