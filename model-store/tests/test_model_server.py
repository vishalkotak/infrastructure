import hashlib
import sys
import types

import pytest

import model_server


def uri_for(blob: bytes) -> str:
    return f"s3://models/fraud-model/{hashlib.sha256(blob).hexdigest()}"


def test_verify_accepts_matching_bytes():
    model_server.verify(b"weights", uri_for(b"weights"))


@pytest.mark.parametrize("blob", [b"weights!", b"", b"other"])
def test_verify_rejects_other_bytes(blob):
    with pytest.raises(ValueError, match="does not match"):
        model_server.verify(blob, uri_for(b"weights"))


@pytest.fixture
def fake_s3(monkeypatch):
    """Stand-in for boto3 that records what fetch asked for."""
    calls = {}

    class Body:
        def read(self):
            return b"weights"

    class Client:
        def get_object(self, Bucket, Key):
            calls["get"] = (Bucket, Key)
            return {"Body": Body()}

    def client(service, **kw):
        calls["client"] = (service, kw)
        return Client()

    boto3 = types.ModuleType("boto3")
    boto3.client = client
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: kw
    botocore = types.ModuleType("botocore")
    botocore.config = config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    monkeypatch.setenv("S3_ENDPOINT", "http://localhost:9000")
    return calls


def test_fetch_splits_bucket_and_key(fake_s3):
    assert model_server.fetch("s3://models/fraud-model/abc123") == b"weights"
    assert fake_s3["get"] == ("models", "fraud-model/abc123")


def test_fetch_uses_s3_endpoint(fake_s3):
    model_server.fetch("s3://models/fraud-model/abc123")
    service, kw = fake_s3["client"]
    assert service == "s3" and kw["endpoint_url"] == "http://localhost:9000"


def test_fetch_then_verify(fake_s3):
    uri = uri_for(b"weights")
    model_server.verify(model_server.fetch(uri), uri)