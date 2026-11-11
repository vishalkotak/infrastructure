from __future__ import annotations

import hashlib
import io
import logging
import os
import random
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated

import torch
from fastapi import FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from demo_model import IN_FEATURES, build

log = logging.getLogger("model-server")
ARTIFACT_URI = os.environ.get("ARTIFACT_URI", "")
WARMUP_PASSES = int(os.environ.get("WARMUP_PASSES", "200"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "256"))

torch.set_num_threads(int(os.environ.get("TORCH_THREADS", "1")))
state = {"model": None, "faults": {}, "ready": False, "load_seconds": None}
LABELS = {"endpoint": os.environ.get("MODEL_ENDPOINT", "local"),
          "role": os.environ.get("MODEL_ROLE", "stable"),
          "version": os.environ.get("MODEL_VERSION", "0")}
NAMES = list(LABELS)
REQUESTS = Counter("model_requests_total", "Prediction requests", NAMES + ["code"])
LATENCY = Histogram("model_request_latency_seconds", "Prediction latency", NAMES + ["code"],
                    buckets=(0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2))
OUTPUTS = Histogram("model_output", "Predicted values", NAMES,
                    buckets=(-3, -2, -1, -0.5, 0, 0.5, 1, 2, 3, 5))
LOAD_SECONDS = Gauge("model_load_seconds", "Time to fetch, verify, load and warm up", NAMES)

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
    LOAD_SECONDS.labels(**LABELS).set(state["load_seconds"])
    log.info("loaded %s in %.1fs", ARTIFACT_URI, state["load_seconds"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    if not ARTIFACT_URI:
        raise RuntimeError("ARTIFACT_URI is not set")
    threading.Thread(target=_startup, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

@app.exception_handler(RequestValidationError)
def bad_request(request, exc: RequestValidationError):
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/ready")
def ready():
    if not state["ready"]:
        raise HTTPException(503, "loading")
    return {"ready": True, "load_seconds": state["load_seconds"]}


Number = Annotated[float, Field(allow_inf_nan=False)]
Row = Annotated[list[Number], Field(min_length=IN_FEATURES, max_length=IN_FEATURES)]


class PredictIn(BaseModel):
    inputs: list[Row] = Field(min_length=1, max_length=MAX_BATCH)


@app.post("/predict")
def predict(body: PredictIn):
    t0 = time.perf_counter()
    code = 200
    try:
        if not state["ready"]:
            raise HTTPException(503, "loading")
        faults = state["faults"]
        if faults.get("latency_ms"):
            time.sleep(faults["latency_ms"] / 1000)
        if random.random() < faults.get("error_rate", 0.0):
            raise HTTPException(500, "injected error")
        with torch.inference_mode():
            y = state["model"](torch.tensor(body.inputs)).squeeze(-1) + faults.get("output_bias", 0.0)
        outputs = y.tolist()
        for v in outputs:
            OUTPUTS.labels(**LABELS).observe(v)
        return {"outputs": outputs}
    except HTTPException as e:
        code = e.status_code
        raise
    except Exception:
        code = 500
        raise
    finally:
        REQUESTS.labels(**LABELS, code=str(code)).inc()
        LATENCY.labels(**LABELS, code=str(code)).observe(time.perf_counter() - t0)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)