import hashlib

import pytest

from modelstore.artifacts import artifact_uri, sha256_file


def test_sha256_file_matches_hashlib(tmp_path):
    data = b"weights" * 3_000_000  # ~21 MB, spans several chunks
    p = tmp_path / "model.pt"
    p.write_bytes(data)
    assert sha256_file(str(p)) == (hashlib.sha256(data).hexdigest(), len(data))


def test_empty_file(tmp_path):
    p = tmp_path / "empty.pt"
    p.write_bytes(b"")
    sha, size = sha256_file(str(p))
    assert size == 0 and len(sha) == 64


def test_missing_file():
    with pytest.raises(FileNotFoundError):
        sha256_file("/no/such/model.pt")


def test_uri_ignores_filename():
    assert artifact_uri("fraud-model", "a" * 64) == "s3://models/fraud-model/" + "a" * 64