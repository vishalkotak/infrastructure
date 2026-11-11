from __future__ import annotations

import logging

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

import render

GROUP, VERSION, PLURAL = "modelstore.dev", "v1", "modelendpoints"


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    settings.posting.level = logging.WARNING


def apply(obj: dict) -> str:
    kind, name, ns = obj["kind"], obj["metadata"]["name"], obj["metadata"]["namespace"]
    if kind == "Deployment":
        api = client.AppsV1Api()
        create, patch = api.create_namespaced_deployment, api.patch_namespaced_deployment
    else:
        api = client.CoreV1Api()
        create, patch = api.create_namespaced_service, api.patch_namespaced_service
    try:
        create(ns, obj)
        return "created"
    except ApiException as e:
        if e.status != 409:
            raise
    patch(name, ns, obj)
    return "updated"


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, name, namespace, body, logger, **_):
    spec = dict(spec)
    for role in render.ROLES:
        for obj in (render.deployment(name, namespace, role, spec), render.service(name, namespace, role)):
            kopf.adopt(obj, owner=body)
            result = apply(obj)
            logger.info("%s %s/%s", result, obj["kind"], obj["metadata"]["name"])