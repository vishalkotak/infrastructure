import logging

import pytest

pytest.importorskip("kopf")
pytest.importorskip("kubernetes")

from kubernetes.client.rest import ApiException  # noqa: E402

import controller  # noqa: E402

STABLE = {"version": 1, "modelRef": "fraud-model@1", "artifactUri": "s3://models/fraud-model/" + "1" * 64}
BODY = {"apiVersion": "modelstore.dev/v1", "kind": "ModelEndpoint",
        "metadata": {"name": "fraud", "namespace": "models", "uid": "uid-123"}}


class FakeAPI:
    """Stands in for AppsV1Api and CoreV1Api, keyed by (kind, name)."""

    def __init__(self, store, kind):
        self.store, self.kind = store, kind

    def _create(self, ns, body):
        key = (self.kind, body["metadata"]["name"])
        if key in self.store:
            raise ApiException(status=409, reason="AlreadyExists")
        self.store[key] = ("created", body)

    def _patch(self, name, ns, body):
        self.store[(self.kind, name)] = ("patched", body)

    create_namespaced_deployment = create_namespaced_service = _create
    patch_namespaced_deployment = patch_namespaced_service = _patch


@pytest.fixture
def cluster(monkeypatch):
    store = {}
    monkeypatch.setattr(controller.client, "AppsV1Api", lambda: FakeAPI(store, "Deployment"))
    monkeypatch.setattr(controller.client, "CoreV1Api", lambda: FakeAPI(store, "Service"))
    return store


def run(spec):
    controller.reconcile(spec=spec, name="fraud", namespace="models", body=BODY,
                         logger=logging.getLogger("test"))


def test_first_reconcile_creates_everything(cluster):
    run({"replicas": 2, "stable": STABLE})
    assert sorted(cluster) == [("Deployment", "fraud-canary"), ("Deployment", "fraud-stable"),
                               ("Service", "fraud-canary"), ("Service", "fraud-stable")]
    assert all(action == "created" for action, _ in cluster.values())
    assert cluster[("Deployment", "fraud-stable")][1]["spec"]["replicas"] == 2
    assert cluster[("Deployment", "fraud-canary")][1]["spec"]["replicas"] == 0


def test_second_reconcile_updates(cluster):
    run({"replicas": 2, "stable": STABLE})
    run({"replicas": 3, "stable": {**STABLE, "version": 2}})
    action, dep = cluster[("Deployment", "fraud-stable")]
    assert action == "patched" and dep["spec"]["replicas"] == 3
    assert dep["spec"]["template"]["metadata"]["labels"]["modelstore.dev/version"] == "2"


def test_children_are_owned_by_the_endpoint(cluster):
    run({"replicas": 1, "stable": STABLE})
    for _, obj in cluster.values():
        owner = obj["metadata"]["ownerReferences"][0]
        assert owner["kind"] == "ModelEndpoint" and owner["uid"] == "uid-123" and owner["controller"] is True


def test_other_api_errors_are_raised(monkeypatch):
    class Broken:
        def create_namespaced_deployment(self, ns, body):
            raise ApiException(status=403, reason="Forbidden")

        def patch_namespaced_deployment(self, name, ns, body):
            raise AssertionError("must not patch after a non-409 error")

    monkeypatch.setattr(controller.client, "AppsV1Api", lambda: Broken())
    with pytest.raises(ApiException):
        run({"replicas": 1, "stable": STABLE})