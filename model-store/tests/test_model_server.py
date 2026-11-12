import hashlib
import sys
import time
import types

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("fastapi")
pytest.importorskip("prometheus_client")

import demo_model
import model_server


def uri_for(blob: bytes) -> str:
    return f"s3://models/fraud-model/{hashlib.sha256(blob).hexdigest()}"


def test_verify_accepts_matching_bytes():
    model_server.verify(b"weights", uri_for(b"weights"))


@pytest.mark.parametrize("blob", [b"weights!", b"", b"other"])
def test_verify_rejects_other_bytes(blob):
    with pytest.raises(ValueError, match="does not match"):
        model_server.verify(blob, uri_for(b"weights"))


@pytest.fixture
def fake_s3(monkeypatch):
    """Stand-in for boto3 that records what fetch asked for."""
    calls = {}

    class Body:
        def read(self):
            return b"weights"

    class Client:
        def get_object(self, Bucket, Key):
            calls["get"] = (Bucket, Key)
            return {"Body": Body()}

    def client(service, **kw):
        calls["client"] = (service, kw)
        return Client()

    boto3 = types.ModuleType("boto3")
    boto3.client = client
    config = types.ModuleType("botocore.config")
    config.Config = lambda **kw: kw
    botocore = types.ModuleType("botocore")
    botocore.config = config
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", config)
    monkeypatch.setenv("S3_ENDPOINT", "http://localhost:9000")
    return calls


def test_fetch_splits_bucket_and_key(fake_s3):
    assert model_server.fetch("s3://models/fraud-model/abc123") == b"weights"
    assert fake_s3["get"] == ("models", "fraud-model/abc123")


def test_fetch_uses_s3_endpoint(fake_s3):
    model_server.fetch("s3://models/fraud-model/abc123")
    service, kw = fake_s3["client"]
    assert service == "s3" and kw["endpoint_url"] == "http://localhost:9000"


def test_fetch_then_verify(fake_s3):
    uri = uri_for(b"weights")
    model_server.verify(model_server.fetch(uri), uri)

def model_bytes(tmp_path, **kw) -> bytes:
    p = tmp_path / "m.pt"
    demo_model.save(str(p), **kw)
    return p.read_bytes()


def to_bytes(obj) -> bytes:
    import io
    buf = io.BytesIO()
    torch.save(obj, buf)
    return buf.getvalue()


def test_load_returns_working_model(tmp_path):
    model, faults = model_server.load_model(model_bytes(tmp_path, seed=1), warmup=0)
    assert faults == {}
    assert not model.training
    assert model(torch.zeros(3, demo_model.IN_FEATURES)).shape == (3, 1)


def test_load_returns_faults(tmp_path):
    _, faults = model_server.load_model(model_bytes(tmp_path, variant="slow"), warmup=0)
    assert faults == {"latency_ms": 120}


def test_file_without_faults(tmp_path):
    blob = to_bytes({"state_dict": demo_model.build().state_dict()})
    assert model_server.load_model(blob, warmup=0)[1] == {}


def test_warmup_runs_the_model(tmp_path, monkeypatch):
    calls = []

    def counted_build():
        m = demo_model.build()
        m.register_forward_hook(lambda *_: calls.append(1))
        return m

    monkeypatch.setattr(model_server, "build", counted_build)
    model_server.load_model(model_bytes(tmp_path), warmup=5)
    assert len(calls) == 5


@pytest.mark.parametrize("blob", [
    b"not a model",
    to_bytes({"weights": {}}),                                         # no state_dict
    to_bytes({"state_dict": torch.nn.Linear(16, 1).state_dict()}),     # wrong architecture
])
def test_bad_files_fail(blob):
    with pytest.raises(Exception):
        model_server.load_model(blob, warmup=0)


class Exited(Exception):
    pass


@pytest.fixture
def startup(monkeypatch, tmp_path):
    """Run _startup against an in-memory artifact; os._exit raises instead of killing pytest."""
    blob = model_bytes(tmp_path, variant="errors")
    monkeypatch.setattr(model_server, "ARTIFACT_URI", uri_for(blob))
    monkeypatch.setattr(model_server, "WARMUP_PASSES", 0)
    monkeypatch.setattr(model_server, "fetch", lambda uri: blob)
    monkeypatch.setattr(model_server, "state", {"model": None, "faults": {}, "ready": False, "load_seconds": None})

    def fake_exit(code):
        raise Exited(code)

    monkeypatch.setattr(model_server.os, "_exit", fake_exit)
    return monkeypatch


def test_startup_marks_ready(startup):
    model_server._startup()
    st = model_server.state
    assert st["ready"] and st["model"] is not None
    assert st["faults"] == {"error_rate": 0.15}
    assert st["load_seconds"] >= 0


def test_startup_exits_on_bad_checksum(startup):
    startup.setattr(model_server, "ARTIFACT_URI", "s3://models/fraud-model/" + "a" * 64)
    with pytest.raises(Exited) as e:
        model_server._startup()
    assert e.value.args == (1,)
    assert not model_server.state["ready"]


def test_startup_exits_when_download_fails(startup):
    def broken(uri):
        raise ConnectionError("minio down")

    startup.setattr(model_server, "fetch", broken)
    with pytest.raises(Exited):
        model_server._startup()
    assert not model_server.state["ready"]


def test_startup_exits_when_load_fails(startup):
    startup.setattr(model_server, "fetch", lambda uri: b"not a model")
    startup.setattr(model_server, "ARTIFACT_URI", uri_for(b"not a model"))
    with pytest.raises(Exited):
        model_server._startup()

@pytest.fixture
def client(startup):
    from fastapi.testclient import TestClient
    return TestClient(model_server.app)  # without `with`, startup code does not run


def test_healthz_always_ok(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_ready_is_503_while_loading(client):
    r = client.get("/ready")
    assert r.status_code == 503 and r.json()["detail"] == "loading"


def test_ready_after_load(client):
    model_server.state.update(ready=True, load_seconds=1.5)
    assert client.get("/ready").json() == {"ready": True, "load_seconds": 1.5}


def test_lifespan_loads_in_background(startup):
    import threading

    from fastapi.testclient import TestClient
    done = threading.Event()

    def fake_startup():
        model_server.state.update(ready=True, load_seconds=0.0)
        done.set()

    startup.setattr(model_server, "_startup", fake_startup)
    with TestClient(model_server.app) as c:
        assert done.wait(timeout=5)
        assert c.get("/ready").status_code == 200


def test_lifespan_refuses_without_artifact(startup):
    from fastapi.testclient import TestClient
    startup.setattr(model_server, "ARTIFACT_URI", "")
    with pytest.raises(RuntimeError, match="ARTIFACT_URI"):
        with TestClient(model_server.app):
            pass


@pytest.fixture
def serving(client, tmp_path):
    """A loaded good model; tests change state["faults"] to misbehave."""
    model, _ = model_server.load_model(model_bytes(tmp_path, seed=1), warmup=0)
    model_server.state.update(model=model, faults={}, ready=True)
    return client


ROW = [0.5] * 16


def expected(rows):
    with torch.inference_mode():
        return model_server.state["model"](torch.tensor(rows)).squeeze(-1).tolist()


def test_predict(serving):
    r = serving.post("/predict", json={"inputs": [ROW, [0.0] * 16]})
    assert r.status_code == 200
    assert r.json()["outputs"] == pytest.approx(expected([ROW, [0.0] * 16]))


def test_predict_not_ready(client):
    assert client.post("/predict", json={"inputs": [ROW]}).status_code == 503


@pytest.mark.parametrize("inputs", [
    [],                       # empty batch
    [[0.5] * 15],             # row too short
    [[0.5] * 17],             # row too long
    [ROW, [0.5] * 15],        # ragged
    [["a"] * 16],             # not numbers
    [ROW] * 257,              # over MAX_BATCH
])
def test_predict_rejects_bad_input(serving, inputs):
    assert serving.post("/predict", json={"inputs": inputs}).status_code == 422


def test_predict_rejects_nan(serving):
    body = '{"inputs": [[NaN' + ", 0.5" * 15 + "]]}"
    r = serving.post("/predict", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_fault_latency(serving):
    model_server.state["faults"] = {"latency_ms": 50}
    t0 = time.monotonic()
    assert serving.post("/predict", json={"inputs": [ROW]}).status_code == 200
    assert time.monotonic() - t0 >= 0.05


def test_fault_errors(serving):
    model_server.state["faults"] = {"error_rate": 1.0}
    r = serving.post("/predict", json={"inputs": [ROW]})
    assert r.status_code == 500 and r.json()["detail"] == "injected error"


def test_fault_output_bias(serving):
    good = expected([ROW])
    model_server.state["faults"] = {"output_bias": 3.0}
    r = serving.post("/predict", json={"inputs": [ROW]})
    assert r.status_code == 200
    assert r.json()["outputs"] == pytest.approx([g + 3.0 for g in good])

def test_predict_rejects_nan(serving):
    body = '{"inputs": [[NaN' + ", 0.5" * 15 + "]]}"
    r = serving.post("/predict", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["type"] == "finite_number" and err["loc"] == ["body", "inputs", 0, 0]
    assert "input" not in err


def test_rejected_batch_is_not_echoed(serving):
    r = serving.post("/predict", json={"inputs": [ROW] * 257})
    assert r.status_code == 422
    assert len(r.content) < 500

def sample(name, **labels):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(name, {**model_server.LABELS, **labels}) or 0.0


def requests(code):
    return sample("model_requests_total", code=str(code))


def test_success_is_counted(serving):
    before = requests(200), sample("model_request_latency_seconds_count", code="200"), sample("model_output_count")
    serving.post("/predict", json={"inputs": [ROW, ROW, ROW]})
    after = requests(200), sample("model_request_latency_seconds_count", code="200"), sample("model_output_count")
    assert [a - b for a, b in zip(after, before)] == [1, 1, 3]


def test_injected_error_is_counted(serving):
    model_server.state["faults"] = {"error_rate": 1.0}
    before = requests(500)
    serving.post("/predict", json={"inputs": [ROW]})
    assert requests(500) - before == 1


def test_not_ready_is_counted(client):
    before = requests(503)
    client.post("/predict", json={"inputs": [ROW]})
    assert requests(503) - before == 1


def test_bad_input_is_not_counted(serving):
    before = sum(requests(c) for c in (200, 422, 500, 503))
    serving.post("/predict", json={"inputs": []})
    assert sum(requests(c) for c in (200, 422, 500, 503)) == before


def test_unexpected_crash_counts_as_500(serving):
    from fastapi.testclient import TestClient

    def broken(x):
        raise RuntimeError("model exploded")

    model_server.state["model"] = broken
    before = requests(500)
    r = TestClient(model_server.app, raise_server_exceptions=False).post("/predict", json={"inputs": [ROW]})
    assert r.status_code == 500
    assert requests(500) - before == 1


def test_load_seconds_recorded(startup):
    model_server._startup()
    assert sample("model_load_seconds") == model_server.state["load_seconds"]


def test_metrics_endpoint(serving):
    from prometheus_client.parser import text_string_to_metric_families
    serving.post("/predict", json={"inputs": [ROW]})
    r = serving.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    samples = [s for fam in text_string_to_metric_families(r.text) for s in fam.samples
               if s.name == "model_requests_total"]
    assert any(s.labels == {**model_server.LABELS, "code": "200"} and s.value >= 1 for s in samples)