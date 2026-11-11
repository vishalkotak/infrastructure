import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
jsonschema = pytest.importorskip("jsonschema")

CRD_PATH = Path(__file__).resolve().parents[1] / "deploy" / "k8s" / "crd.yaml"
CRD = yaml.safe_load(CRD_PATH.read_text())
SCHEMA = CRD["spec"]["versions"][0]["schema"]["openAPIV3Schema"]

URI = "s3://models/fraud-model/" + "a" * 64
STABLE = {"version": 3, "modelRef": "fraud-model@3", "artifactUri": URI}
CANARY = {"version": 4, "modelRef": "fraud-model@4", "artifactUri": URI, "replicas": 1, "weight": 20}


def endpoint(**spec):
    return {"spec": spec}


@pytest.mark.parametrize("obj", [
    endpoint(replicas=2),                                   # created, nothing deployed yet
    endpoint(replicas=2, stable=STABLE),
    endpoint(replicas=2, stable=STABLE, canary=CANARY),
    endpoint(replicas=0, stable=STABLE),                    # scaled to zero
    endpoint(replicas=2, stable=STABLE, canary={**CANARY, "weight": 0}),
])
def test_valid(obj):
    jsonschema.validate(obj, SCHEMA)


@pytest.mark.parametrize("obj", [
    endpoint(),                                                         # replicas required
    endpoint(replicas=-1),
    endpoint(replicas="2"),
    endpoint(replicas=2, stable={**STABLE, "version": 0}),
    endpoint(replicas=2, stable={**STABLE, "artifactUri": "/tmp/m.pt"}),
    endpoint(replicas=2, stable={"version": 3}),                         # missing fields
    endpoint(replicas=2, stable=STABLE, canary={**CANARY, "weight": 101}),
    endpoint(replicas=2, stable=STABLE, canary={**CANARY, "replicas": 0}),
    endpoint(replicas=2, stable=STABLE, canary={k: v for k, v in CANARY.items() if k != "weight"}),
])
def test_invalid(obj):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(obj, SCHEMA)


def test_names_and_status():
    v = CRD["spec"]["versions"][0]
    assert CRD["metadata"]["name"] == "modelendpoints.modelstore.dev"
    assert CRD["spec"]["names"]["kind"] == "ModelEndpoint"
    assert v["subresources"] == {"status": {}}


CONTEXT = os.environ.get("MS_KUBE_CONTEXT")
cluster = pytest.mark.skipif(not CONTEXT, reason="set MS_KUBE_CONTEXT to test against a cluster")


def server_dry_run(spec: dict) -> subprocess.CompletedProcess:
    body = {"apiVersion": "modelstore.dev/v1", "kind": "ModelEndpoint",
            "metadata": {"name": "crd-test", "namespace": "default"}, "spec": spec}
    return subprocess.run(["kubectl", "--context", CONTEXT, "apply", "--dry-run=server", "-f", "-"],
                          input=yaml.safe_dump(body), capture_output=True, text=True)


@cluster
def test_cluster_accepts_valid():
    r = server_dry_run({"replicas": 2, "stable": STABLE, "canary": CANARY})
    assert r.returncode == 0, r.stderr


@cluster
@pytest.mark.parametrize("spec,message", [
    ({"replicas": 2, "canary": CANARY}, "a canary needs a stable version"),
    ({"replicas": 2, "stable": STABLE, "canary": {**CANARY, "version": 3}}, "must be different versions"),
])
def test_cluster_rules(spec, message):
    r = server_dry_run(spec)
    assert r.returncode != 0 and message in r.stderr