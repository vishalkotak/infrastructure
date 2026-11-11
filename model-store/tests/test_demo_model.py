import pytest

torch = pytest.importorskip("torch")

import demo_model  # noqa: E402


def saved(tmp_path, **kw) -> dict:
    p = tmp_path / "m.pt"
    demo_model.save(str(p), **kw)
    return torch.load(p, weights_only=True)


def test_good_has_no_faults(tmp_path):
    d = saved(tmp_path, seed=1)
    assert set(d) == {"state_dict", "faults"}
    assert d["faults"] == {}


@pytest.mark.parametrize("variant", list(demo_model.VARIANTS))
def test_variant_faults_are_saved(tmp_path, variant):
    assert saved(tmp_path, variant=variant)["faults"] == demo_model.VARIANTS[variant]


def test_weights_fit_build(tmp_path):
    model = demo_model.build()
    model.load_state_dict(saved(tmp_path)["state_dict"])
    assert model(torch.zeros(2, demo_model.IN_FEATURES)).shape == (2, 1)


def test_seed_controls_weights(tmp_path):
    w = lambda d: d["state_dict"]["0.weight"]  # noqa: E731
    assert torch.equal(w(saved(tmp_path, seed=1)), w(saved(tmp_path, seed=1)))
    assert not torch.equal(w(saved(tmp_path, seed=1)), w(saved(tmp_path, seed=2)))


def test_unknown_variant(tmp_path):
    with pytest.raises(KeyError):
        demo_model.save(str(tmp_path / "m.pt"), variant="broken")
        