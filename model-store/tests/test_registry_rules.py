import itertools

import pytest

from modelstore.registry_rules import (STAGES, PolicyError, check_artifact, check_deploy, check_metadata,
                                       check_name, check_stage_change, check_tag_change,
                                       check_transition, displaced_stage, parse_ref,
                                       validate_deployable_stages)

D = ("STAGING", "PRODUCTION")


def ok(fn, *a, **k):
    fn(*a, **k)
    return True


def refused(fn, *a, **k):
    with pytest.raises(PolicyError):
        fn(*a, **k)
    return True


# references

@pytest.mark.parametrize("ref,expected", [
    ("fraud-model@3", ("fraud-model", "3")),
    ("fraud-model@003", ("fraud-model", "3")),
    ("Fraud-Model@Production", ("fraud-model", "production")),
    ("  fraud-model@staging ", ("fraud-model", "staging")),
    ("fraud-model@latest", ("fraud-model", "latest")),
    ("a@1", ("a", "1")),
])
def test_parse_ref_ok(ref, expected):
    assert parse_ref(ref) == expected


@pytest.mark.parametrize("ref", ["fraud-model", "fraud-model@", "@3", "fraud_model@3", "fraud-model@v3",
                                 "fraud-model@0", "fraud-model@-1", "-fraud@1", "fraud-@1",
                                 "fraud model@1", "fraud-model@archived", "fraud-model@none", ""])
def test_parse_ref_refused(ref):
    refused(parse_ref, ref)


@pytest.mark.parametrize("name,good", [("fraud-model", True), ("a", True), ("m" * 63, True),
                                       ("m" * 64, False), ("Fraud", False), ("fraud_model", False),
                                       ("-fraud", False), ("fraud-", False), ("", False)])
def test_check_name(name, good):
    assert (ok if good else refused)(check_name, name)


SHA = "a" * 64


def test_check_artifact():
    ok(check_artifact, "s3://models/fraud-model/x/model.pt", SHA, 1024)
    refused(check_artifact, "/tmp/model.pt", SHA, 1024)
    refused(check_artifact, "s3://models/x", "A" * 64, 1024)    # uppercase hex
    refused(check_artifact, "s3://models/x", "abc", 1024)
    refused(check_artifact, "s3://models/x", SHA, 0)


def test_check_metadata():
    ok(check_metadata, {"auc": 0.91, "rows": 1000}, {"dataset": "2026-10", "team.owner": "risk"})
    refused(check_metadata, {"auc": "0.91"}, {})
    refused(check_metadata, {"auc": True}, {})
    refused(check_metadata, {"auc": float("nan")}, {})
    refused(check_metadata, {}, {"data set": "x"})
    refused(check_metadata, {}, {"k=v": "x"})
    refused(check_metadata, {}, {"dataset": 2026})


def test_check_tag_change():
    ok(check_tag_change, {"dataset": "2026-10"}, ["old"])
    ok(check_tag_change, {}, [])
    refused(check_tag_change, {"dataset": "x"}, ["dataset"])
    refused(check_tag_change, {"bad key": "x"}, [])


# config

def test_deployable_policy_validation():
    assert validate_deployable_stages(("staging", " production ")) == ("STAGING", "PRODUCTION")
    assert ok(validate_deployable_stages, ("NONE", "STAGING", "PRODUCTION"))   # dev setups
    assert refused(validate_deployable_stages, ("PRODUCTION",))                 # nothing could reach PROD
    assert refused(validate_deployable_stages, ("STAGING",))
    assert refused(validate_deployable_stages, ("STAGING", "PRODUCTION", "ARCHIVED"))
    assert refused(validate_deployable_stages, ("STAGING", "PRODUCTION", "LIVE"))


# every stage move; columns are idle, deployed, rolling

EXPECT = {
    ("NONE", "NONE"):             ("ok", "ok", "ok"),
    ("NONE", "STAGING"):          ("ok", "ok", "ok"),
    ("NONE", "PRODUCTION"):       ("no", "no", "no"),   # not in lifecycle
    ("NONE", "ARCHIVED"):         ("ok", "no", "no"),   # nothing live may be archived
    ("STAGING", "NONE"):          ("no", "no", "no"),
    ("STAGING", "STAGING"):       ("ok", "ok", "ok"),
    ("STAGING", "PRODUCTION"):    ("no", "ok", "no"),   # needs a completed rollout
    ("STAGING", "ARCHIVED"):      ("ok", "no", "no"),
    ("PRODUCTION", "NONE"):       ("no", "no", "no"),
    ("PRODUCTION", "STAGING"):    ("ok", "ok", "ok"),   # demotion: always allowed
    ("PRODUCTION", "PRODUCTION"): ("ok", "ok", "ok"),
    ("PRODUCTION", "ARCHIVED"):   ("ok", "no", "no"),
    ("ARCHIVED", "NONE"):         ("no", "no", "no"),
    ("ARCHIVED", "STAGING"):      ("ok", "ok", "ok"),   # re-qualify (also after a rollback to it)
    ("ARCHIVED", "PRODUCTION"):   ("no", "no", "no"),
    ("ARCHIVED", "ARCHIVED"):     ("ok", "ok", "ok"),
}
STATES = {"idle": ([], []), "deployed": (["fraud"], ["fraud"]), "rolling": (["fraud"], [])}


def test_table_covers_every_pair():
    assert set(EXPECT) == set(itertools.product(STAGES, STAGES))


@pytest.mark.parametrize("current,target", sorted(EXPECT))
@pytest.mark.parametrize("state_i,state", list(enumerate(STATES)))
def test_every_move(current, target, state_i, state):
    serving, deployed = STATES[state]
    want = EXPECT[(current, target)][state_i]
    call = (check_stage_change, current, target)
    kw = dict(serving_on=serving, deployed_on=deployed, deployable=D)
    assert (ok if want == "ok" else refused)(*call, **kw)


@pytest.mark.parametrize("current,target", sorted(EXPECT))
def test_force_overrides_guards_but_never_the_lifecycle(current, target):
    kw = dict(serving_on=["fraud"], deployed_on=[], deployable=D, force=True)
    in_lifecycle = current == target or target in {"NONE": {"STAGING", "ARCHIVED"},
                                                   "STAGING": {"PRODUCTION", "ARCHIVED"},
                                                   "PRODUCTION": {"STAGING", "ARCHIVED"},
                                                   "ARCHIVED": {"STAGING"}}[current]
    assert (ok if in_lifecycle else refused)(check_stage_change, current, target, **kw)


def test_messages_say_what_to_do():
    with pytest.raises(PolicyError, match="from NONE you can go to ARCHIVED, STAGING"):
        check_transition("NONE", "PRODUCTION")
    with pytest.raises(PolicyError, match="rollout still in progress on fraud"):
        check_stage_change("STAGING", "PRODUCTION", serving_on=["fraud"], deployed_on=[], deployable=D)
    with pytest.raises(PolicyError, match="still serving on fraud, fraud-eu"):
        check_stage_change("PRODUCTION", "ARCHIVED", serving_on=["fraud-eu", "fraud"],
                           deployed_on=["fraud"], deployable=D)
    refused(check_stage_change, "STAGING", "BOGUS", serving_on=[], deployed_on=[], deployable=D)


def test_lowercase_target_accepted():
    check_stage_change("STAGING", "production", serving_on=["fraud"], deployed_on=["fraud"], deployable=D)


def test_none_deployable_dev_policy():
    dev = ("NONE", "STAGING", "PRODUCTION")
    refused(check_stage_change, "NONE", "ARCHIVED", serving_on=["dev"], deployed_on=["dev"], deployable=dev)
    refused(check_stage_change, "NONE", "PRODUCTION", serving_on=["dev"], deployed_on=["dev"], deployable=dev)


# displacement on promotion

def test_displaced_version():
    assert displaced_stage([], archive_existing=True) == "ARCHIVED"
    assert displaced_stage([], archive_existing=False) == "STAGING"
    assert displaced_stage(["fraud-eu"], archive_existing=True) == "STAGING"   # still live: stay deployable
    assert displaced_stage(["fraud-eu"], archive_existing=False) == "STAGING"


# deploys and rollbacks

def deploy(kind="DEPLOY", stage="STAGING", here=False, ep_model="fraud-model", v_model="fraud-model", dep=D):
    return check_deploy(kind, endpoint_model=ep_model, version_model=v_model, version_ref=f"{v_model}@4",
                        stage=stage, deployable=dep, already_serving_here=here)


@pytest.mark.parametrize("stage,want", [("NONE", "no"), ("STAGING", "ok"), ("PRODUCTION", "ok"), ("ARCHIVED", "no")])
def test_deploy_by_stage(stage, want):
    assert (ok if want == "ok" else refused)(deploy, stage=stage)


def test_deploy_none_allowed_by_dev_policy():
    ok(deploy, stage="NONE", dep=("NONE", "STAGING", "PRODUCTION"))


def test_deploy_refusals():
    with pytest.raises(PolicyError, match="already serving on this endpoint"):
        deploy(here=True)
    with pytest.raises(PolicyError, match="serves model fraud-model"):
        deploy(v_model="churn-model")
    with pytest.raises(PolicyError, match="stage it first"):
        deploy(stage="NONE")
    with pytest.raises(PolicyError, match="re-qualify"):
        deploy(stage="ARCHIVED")
    refused(deploy, kind="REDEPLOY")


@pytest.mark.parametrize("stage", STAGES)
def test_rollback_ignores_stage(stage):
    ok(deploy, kind="ROLLBACK", stage=stage)


def test_rollback_still_checks_model():
    refused(deploy, kind="ROLLBACK", v_model="churn-model")