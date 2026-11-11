from __future__ import annotations

import hashlib
import os


def fetch(uri: str) -> bytes:
    import boto3
    from botocore.config import Config
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    s3 = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT"), region_name="us-east-1",
                      config=Config(signature_version="s3v4"))
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def verify(blob: bytes, uri: str) -> None:
    expected = uri.rsplit("/", 1)[-1]
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ValueError(f"artifact checksum {actual} does not match {expected}")