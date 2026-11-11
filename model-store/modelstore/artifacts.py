from __future__ import annotations

import hashlib

from .config import settings
from .registry_rules import check_artifact

CHUNK = 8 * 1024 * 1024


def sha256_file(path: str) -> tuple[str, int]:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def artifact_uri(model: str, sha256: str) -> str:
    return f"s3://{settings.s3_bucket}/{model}/{sha256}"


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=settings.s3_endpoint, region_name="us-east-1",
                        aws_access_key_id=settings.s3_key, aws_secret_access_key=settings.s3_secret,
                        config=Config(signature_version="s3v4"))


def ensure_bucket() -> None:
    from botocore.exceptions import ClientError
    s3 = _s3()
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        s3.create_bucket(Bucket=settings.s3_bucket)


def upload(model: str, path: str) -> tuple[str, str, int]:
    """Returns (uri, sha256, size). Skips the upload if the same bytes are already stored."""
    from botocore.exceptions import ClientError
    sha, size = sha256_file(path)
    uri = artifact_uri(model, sha)
    check_artifact(uri, sha, size)
    key = uri.removeprefix(f"s3://{settings.s3_bucket}/")
    s3 = _s3()
    try:
        s3.head_object(Bucket=settings.s3_bucket, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
            raise
        s3.upload_file(path, settings.s3_bucket, key, ExtraArgs={"Metadata": {"sha256": sha}})
    return uri, sha, size