from __future__ import annotations

import os

IMAGE = os.environ.get("MODEL_SERVER_IMAGE", "modelstore/model-server:dev")
ROLES = ("stable", "canary")
PORT = 8080
DEFAULT_S3 = "http://host.minikube.internal:9000"

READINESS = {"httpGet": {"path": "/ready", "port": PORT}, "periodSeconds": 2, "failureThreshold": 2}
LIVENESS = {"httpGet": {"path": "/healthz", "port": PORT}, "initialDelaySeconds": 10, "periodSeconds": 10}
RESOURCES = {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}}
SURGE_FIRST = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}


def selector(endpoint: str, role: str) -> dict:
    return {"app": "model-server", "modelstore.dev/endpoint": endpoint, "modelstore.dev/role": role}


def desired_replicas(spec: dict, role: str) -> int:
    if role == "stable":
        return spec["replicas"] if "stable" in spec else 0
    return spec["canary"]["replicas"] if "canary" in spec else 0


def server_env(endpoint: str, role: str, spec: dict) -> list[dict]:
    model = spec.get(role, {})
    env = {
        "MODEL_ENDPOINT": endpoint,
        "MODEL_ROLE": role,
        "MODEL_VERSION": str(model.get("version", 0)),
        "ARTIFACT_URI": model.get("artifactUri", ""),
        "S3_ENDPOINT": spec.get("s3Endpoint", DEFAULT_S3),
    }
    return [{"name": k, "value": v} for k, v in env.items()]


def container(endpoint: str, role: str, spec: dict) -> dict:
    return {
        "name": "server",
        "image": IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": PORT}],
        "env": server_env(endpoint, role, spec),
        "envFrom": [{"secretRef": {"name": "s3-credentials"}}],
        "readinessProbe": READINESS,
        "livenessProbe": LIVENESS,
        "lifecycle": {"preStop": {"exec": {"command": ["sleep", "5"]}}},
        "resources": RESOURCES,
    }


def pod_template(endpoint: str, role: str, spec: dict) -> dict:
    version = str(spec.get(role, {}).get("version", 0))
    return {
        "metadata": {
            "labels": {**selector(endpoint, role), "modelstore.dev/version": version},
            "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": str(PORT)},
        },
        "spec": {"terminationGracePeriodSeconds": 20, "containers": [container(endpoint, role, spec)]},
    }


def deployment(endpoint: str, namespace: str, role: str, spec: dict) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"{endpoint}-{role}", "namespace": namespace, "labels": selector(endpoint, role)},
        "spec": {
            "replicas": desired_replicas(spec, role),
            "selector": {"matchLabels": selector(endpoint, role)},
            "progressDeadlineSeconds": 300,
            "strategy": SURGE_FIRST,
            "template": pod_template(endpoint, role, spec),
        },
    }


def service(endpoint: str, namespace: str, role: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"{endpoint}-{role}", "namespace": namespace, "labels": selector(endpoint, role)},
        "spec": {"selector": selector(endpoint, role),
                 "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]},
    }