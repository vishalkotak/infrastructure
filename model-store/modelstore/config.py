"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_url: str = _env("MS_DB_URL", "postgresql+psycopg://modelstore:modelstore@localhost:5432/modelstore")
    temporal_addr: str = _env("MS_TEMPORAL", "localhost:7233")
    task_queue: str = _env("MS_TASK_QUEUE", "model-store")
    namespace: str = _env("MS_K8S_NAMESPACE", "models")

    dcs: tuple[str, ...] = tuple(_env("MS_DCS", "dc-a,dc-b").split(","))
    prom_urls: dict = field(default_factory=lambda: {
        dc: os.environ.get(f"MS_PROM_{dc.upper().replace('-', '_')}", url)
        for dc, url in zip(_env("MS_DCS", "dc-a,dc-b").split(","),
                           ("http://localhost:9091", "http://localhost:9092", "http://localhost:9093"))
    })

    s3_endpoint: str = _env("MS_S3_ENDPOINT", "http://localhost:9000")
    s3_endpoint_in_cluster: str = _env("MS_S3_ENDPOINT_IN_CLUSTER", "http://host.minikube.internal:9000")
    s3_bucket: str = _env("MS_S3_BUCKET", "models")
    s3_key: str = _env("MS_S3_KEY", "minioadmin")
    s3_secret: str = _env("MS_S3_SECRET", "minioadmin")
    max_artifact_bytes: int = int(_env("MS_MAX_ARTIFACT_BYTES", str(20 * 1024 ** 3)))

    deployable_stages: tuple[str, ...] = tuple(
        s.strip().upper() for s in _env("MS_DEPLOYABLE_STAGES", "STAGING,PRODUCTION").split(","))

    warmup_s: int = int(_env("MS_WARMUP_S", "20"))
    canary_window_s: int = int(_env("MS_CANARY_WINDOW_S", "60"))
    canary_max_windows: int = int(_env("MS_CANARY_MAX_WINDOWS", "6"))
    soak_s: int = int(_env("MS_SOAK_S", "60"))
    ready_timeout_s: int = int(_env("MS_READY_TIMEOUT_S", "600"))


settings = Settings()


def op_workflow_id(endpoint: str) -> str:
    return f"endpoint-op-{endpoint}"