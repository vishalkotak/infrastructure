import render

URI3 = "s3://models/fraud-model/" + "3" * 64
URI4 = "s3://models/fraud-model/" + "4" * 64
STABLE = {"version": 7, "modelRef": "fraud-model@3", "artifactUri": URI3}
CANARY = {"version": 8, "modelRef": "fraud-model@4", "artifactUri": URI4, "replicas": 1, "weight": 20}


def env_of(dep):
    return {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}


def test_stable_deployment():
    d = render.deployment("fraud", "models", "stable", {"replicas": 2, "stable": STABLE})
    assert d["metadata"]["name"] == "fraud-stable"
    assert d["spec"]["replicas"] == 2
    assert env_of(d) == {"MODEL_ENDPOINT": "fraud", "MODEL_ROLE": "stable", "MODEL_VERSION": "7",
                         "ARTIFACT_URI": URI3, "S3_ENDPOINT": "http://host.minikube.internal:9000"}
    assert d["spec"]["template"]["metadata"]["labels"]["modelstore.dev/version"] == "7"


def test_canary_deployment():
    spec = {"replicas": 2, "stable": STABLE, "canary": CANARY}
    d = render.deployment("fraud", "models", "canary", spec)
    assert d["metadata"]["name"] == "fraud-canary"
    assert d["spec"]["replicas"] == 1
    assert env_of(d)["ARTIFACT_URI"] == URI4 and env_of(d)["MODEL_VERSION"] == "8"


def test_absent_roles_scale_to_zero():
    assert render.deployment("fraud", "models", "stable", {"replicas": 2})["spec"]["replicas"] == 0
    assert render.deployment("fraud", "models", "canary", {"replicas": 2, "stable": STABLE})["spec"]["replicas"] == 0


def test_selector_never_includes_version():
    a = render.deployment("fraud", "models", "stable", {"replicas": 2, "stable": STABLE})
    b = render.deployment("fraud", "models", "stable", {"replicas": 2, "stable": {**STABLE, "version": 9}})
    assert a["spec"]["selector"] == b["spec"]["selector"]
    assert "modelstore.dev/version" not in a["spec"]["selector"]["matchLabels"]
    assert a["spec"]["template"]["metadata"]["labels"] != b["spec"]["template"]["metadata"]["labels"]


def test_template_labels_match_selector():
    d = render.deployment("fraud", "models", "canary", {"replicas": 2, "stable": STABLE, "canary": CANARY})
    labels = d["spec"]["template"]["metadata"]["labels"]
    assert d["spec"]["selector"]["matchLabels"].items() <= labels.items()


def test_rollout_never_drops_capacity():
    d = render.deployment("fraud", "models", "stable", {"replicas": 2, "stable": STABLE})
    assert d["spec"]["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}


def test_s3_endpoint_override():
    spec = {"replicas": 1, "stable": STABLE, "s3Endpoint": "http://minio.storage:9000"}
    assert env_of(render.deployment("fraud", "models", "stable", spec))["S3_ENDPOINT"] == "http://minio.storage:9000"


def test_same_spec_same_output():
    spec = {"replicas": 2, "stable": STABLE, "canary": CANARY}
    assert render.deployment("fraud", "models", "stable", spec) == render.deployment("fraud", "models", "stable", spec)


def test_service_targets_its_role_only():
    stable = render.service("fraud", "models", "stable")
    canary = render.service("fraud", "models", "canary")
    assert stable["metadata"]["name"] == "fraud-stable" and canary["metadata"]["name"] == "fraud-canary"
    assert stable["spec"]["selector"]["modelstore.dev/role"] == "stable"
    assert canary["spec"]["selector"]["modelstore.dev/role"] == "canary"


def test_service_selects_the_deployments_pods():
    spec = {"replicas": 2, "stable": STABLE, "canary": CANARY}
    for role in render.ROLES:
        pod_labels = render.deployment("fraud", "models", role, spec)["spec"]["template"]["metadata"]["labels"]
        assert render.service("fraud", "models", role)["spec"]["selector"].items() <= pod_labels.items()


def test_service_port_points_at_container_port():
    port = render.service("fraud", "models", "stable")["spec"]["ports"][0]
    container = render.deployment("fraud", "models", "stable", {"replicas": 1, "stable": STABLE})
    names = [p["name"] for p in container["spec"]["template"]["spec"]["containers"][0]["ports"]]
    assert port["port"] == 8080 and port["targetPort"] in names