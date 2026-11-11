from __future__ import annotations

import hashlib
import io
import logging
import os
import time

import torch

from demo_model import IN_FEATURES, build

log = logging.getLogger("model-server")
ARTIFACT_URI = os.environ.get("ARTIFACT_URI", "")
WARMUP_PASSES = int(os.environ.get("WARMUP_PASSES", "200"))

torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "1")))
state = {"model": None, "faults": {}, "ready": False, "load_seconds": None}

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


def load_model(blob: bytes, warmup: int) -> tuple[torch.nn.Module, dict]:
    saved = torch.load(io.BytesIO(blob), weights_only=True)
    model = build()
    model.load_state_dict(saved["state_dict"])
    model.eval()
    with torch.inference_mode():
        for _ in range(warmup):
            model(torch.randn(8, IN_FEATURES))
    return model, saved.get("faults", {})


def _startup() -> None:
    t0 = time.monotonic()
    try:
        blob = fetch(ARTIFACT_URI)
        verify(blob, ARTIFACT_URI)
        model, faults = load_model(blob, WARMUP_PASSES)
    except Exception:
        log.exception("failed to load %s", ARTIFACT_URI)
        os._exit(1)  # crash so Kubernetes reports it
    state.update(model=model, faults=faults, ready=True, load_seconds=time.monotonic() - t0)
    log.info("loaded %s in %.1fs", ARTIFACT_URI, state["load_seconds"])