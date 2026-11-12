import pytest

pytest.importorskip("sqlalchemy")

from conftest import sha, uri  # noqa: E402

from modelstore import artifacts, registry  # noqa: E402
from modelstore.registry_rules import PolicyError  # noqa: E402


@pytest.fixture
def uploads(monkeypatch):
    """Replace the S3 upload with a fake that records calls."""
    calls = []

    def fake_upload(model, path):
        calls.append((model, path))
        n = len(open(path, "rb").read())
        return uri(model, n), sha(n), n

    monkeypatch.setattr(artifacts, "upload", fake_upload)
    return calls


def test_publish(fresh_db, uploads, tmp_path):
    fresh_db.create_model("fraud-model", "risk-team")
    f = tmp_path / "v1.pt"
    f.write_bytes(b"x" * 10)
    v, created = registry.publish("fraud-model", str(f), "vishal", metrics={"auc": 0.9})
    assert created and v.ref == "fraud-model@1" and v.metrics == {"auc": 0.9}
    assert registry.publish("fraud-model", str(f), "vishal")[1] is False


def test_nothing_uploaded_on_early_failure(fresh_db, uploads, tmp_path):
    f = tmp_path / "v1.pt"
    f.write_bytes(b"x")
    with pytest.raises(fresh_db.NotFound):
        registry.publish("nope", str(f), "vishal")
    fresh_db.create_model("fraud-model", "a")
    with pytest.raises(PolicyError):
        registry.publish("fraud-model", str(f), "vishal", metrics={"auc": "high"})
    assert uploads == []