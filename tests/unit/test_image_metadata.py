import json
from unittest.mock import patch
import urllib.error

import pytest

from calunga_release_watcher import image_metadata
from calunga_release_watcher.tracker import PipelineState, PipelineTracker

IMAGE = "quay.io/redhat-user-workloads/lightwell-poc-tenant/component/component@sha256:" + "a" * 64
GAV = "org.javassist:javassist:3.31.0.GA-rhlw-00000-n-00010"


@pytest.fixture
def proxy(monkeypatch):
    monkeypatch.setattr(image_metadata, "IMAGE_METADATA_PROXY_URL", "https://proxy.example.com")
    monkeypatch.setattr(image_metadata, "TENANT_NAMESPACE", "lightwell-poc-tenant")
    monkeypatch.setenv("K8S_TOKEN", "test-token")
    with patch.object(image_metadata.urllib.request, "build_opener") as build:
        response = build.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"annotations": {
            "dev.lightwell.primary-gav": GAV,
            "dev.lightwell.gav-count": "1",
        }}).encode()
        yield build, response


def test_reads_annotations_with_existing_token_without_layers(proxy):
    build, _ = proxy
    assert image_metadata.get_gav(IMAGE) == GAV
    request = build.return_value.open.call_args.args[0]
    assert request.full_url == "https://proxy.example.com/v2/" + IMAGE.split("/", 1)[1].replace("@", "/manifests/")
    assert request.get_header("Authorization") == "Bearer test-token"
    assert build.return_value.open.call_args.kwargs == {"timeout": 5}
    assert isinstance(build.call_args.args[0], image_metadata._NoRedirect)
    assert build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://other") is None


@pytest.mark.parametrize("image", [
    IMAGE.replace("lightwell-poc-tenant", "other-tenant"),
    IMAGE.replace("quay.io", "attacker.example.com"),
    IMAGE.split("@")[0] + ":latest",
    "",
])
def test_rejects_untrusted_or_mutable_references(proxy, image):
    assert image_metadata.get_gav(image) == ""
    proxy[0].assert_not_called()


def test_disabled_by_default(proxy, monkeypatch):
    monkeypatch.setattr(image_metadata, "IMAGE_METADATA_PROXY_URL", "")
    assert image_metadata.get_gav(IMAGE) == ""
    proxy[0].assert_not_called()


@pytest.mark.parametrize("annotations,expected", [
    ({}, ""),
    ({"dev.lightwell.primary-gav": "<@everyone>"}, ""),
    ({"dev.lightwell.primary-gav": GAV, "dev.lightwell.gav-count": "3"}, GAV + " (+2 additional artifacts)"),
])
def test_missing_unsafe_and_multiple_gavs(proxy, annotations, expected):
    proxy[1].read.return_value = json.dumps({"annotations": annotations}).encode()
    assert image_metadata.get_gav(IMAGE) == expected


@pytest.mark.parametrize("failure", [TimeoutError(), urllib.error.HTTPError("https://proxy.example.com", 401, "Unauthorized", {}, None)])
def test_lookup_failure_falls_back_without_logging_token(proxy, failure, caplog):
    proxy[0].return_value.open.side_effect = failure
    assert image_metadata.get_gav(IMAGE) == ""
    assert "test-token" not in caplog.text


def test_malformed_or_oversized_manifest(proxy):
    oversized = json.dumps({"annotations": {"dev.lightwell.primary-gav": GAV},
                            "padding": "x" * (1024 * 1024)}).encode()
    for raw in [b"not JSON", oversized, b'[]']:
        proxy[1].read.return_value = raw
        assert image_metadata.get_gav(IMAGE) == ""


@pytest.mark.parametrize("gav", [GAV, ""])
def test_snapshot_image_reaches_success_message(gav):
    tracker = PipelineTracker()
    body = {
        "metadata": {"name": "snapshot", "namespace": "lightwell-poc-tenant", "labels": {
            "appstudio.openshift.io/build-pipelinerun": "build-1",
            "appstudio.openshift.io/application": "pnc-import-novel",
        }},
        "spec": {"components": [{"containerImage": IMAGE}]},
    }
    tracker.on_snapshot(body)
    info = tracker.get("build-1")
    info.release_pipelinerun = "release-namespace/managed-1"
    tracker._live = True
    with patch("calunga_release_watcher.tracker.get_gav", return_value=gav) as lookup, patch("calunga_release_watcher.tracker._fire_slack") as slack:
        tracker._transition(info, PipelineState.RELEASED)
    lookup.assert_called_once_with(IMAGE)
    message = slack.call_args.args[0]
    assert message.split(" — pipeline complete.")[0] == "✅ " + (GAV if gav else "snapshot (build-1)")
    assert "Application: pnc-import-novel." in message


def test_multi_component_snapshot_does_not_use_one_components_gav():
    tracker = PipelineTracker()
    tracker.on_snapshot({
        "metadata": {"name": "snapshot", "namespace": "tenant", "labels": {
            "appstudio.openshift.io/build-pipelinerun": "build-1",
        }},
        "spec": {"components": [{"containerImage": IMAGE}, {"containerImage": IMAGE}]},
    })
    assert tracker.get("build-1").input_image == ""


def test_malformed_proxy_url_falls_back(proxy, monkeypatch):
    monkeypatch.setattr(image_metadata, "IMAGE_METADATA_PROXY_URL", "https://[broken")
    assert image_metadata.get_gav(IMAGE) == ""
    proxy[0].assert_not_called()


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("managed", [False, True])
def test_success_uses_referenced_snapshot_even_if_latest_differs(monkeypatch, cached, managed):
    from calunga_release_watcher import tracker as module
    monkeypatch.setattr(module, "IMAGE_METADATA_PROXY_URL", "https://proxy.example.com")
    monkeypatch.setattr(module, "TENANT_NAMESPACE", "lightwell-poc-tenant")
    monkeypatch.setattr(module, "KONFLUX_UI_URL", "")
    tracker = PipelineTracker()
    tracker._live = True
    snapshot = {"spec": {"components": [{"containerImage": IMAGE}]}}
    if cached:
        tracker._snapshot_images[("lightwell-poc-tenant", "released-snapshot")] = IMAGE
    info = tracker.get_or_create("sha", {"metadata": {"name": "build"}})
    info.input_image = "wrong-latest-image"
    info.snapshot = "different-snapshot"
    body = {
        "kind": "PipelineRun" if managed else "Release",
        "metadata": {"name": "managed" if managed else "release", "namespace": "lightwell-poc-tenant", "labels": {
            "pac.test.appstudio.openshift.io/sha": "sha",
            ("appstudio.openshift.io/snapshot" if managed else "release.appstudio.openshift.io/snapshot"): "released-snapshot",
        }},
        "status": {"conditions": [{"type": "Succeeded" if managed else "Released", "status": "True"}]},
    }
    with patch.object(module, "_read_resource", return_value=snapshot) as read, patch.object(module, "get_gav", return_value=GAV) as lookup, patch.object(module, "_fire_slack") as slack:
        (tracker.on_release_pipelinerun if managed else tracker.on_release)(body)
    lookup.assert_called_once_with(IMAGE)
    assert slack.call_args.args[0].startswith("✅ " + GAV + " —")
    if cached:
        read.assert_not_called()
    else:
        read.assert_called_once_with("snapshots", "released-snapshot", "lightwell-poc-tenant")


def test_missing_snapshot_falls_back_without_using_unrelated_image(monkeypatch):
    from calunga_release_watcher import tracker as module
    monkeypatch.setattr(module, "IMAGE_METADATA_PROXY_URL", "https://proxy.example.com")
    monkeypatch.setattr(module, "KONFLUX_UI_URL", "")
    tracker = PipelineTracker()
    tracker._live = True
    info = tracker.get_or_create("sha", {"metadata": {"name": "build"}})
    info.input_image = IMAGE
    with patch.object(module, "_read_resource", return_value={}), patch.object(module, "get_gav") as lookup, patch.object(module, "_fire_slack") as slack:
        tracker._transition(info, PipelineState.RELEASED, body={"spec": {"snapshot": "unavailable"}})
    lookup.assert_not_called()
    assert slack.call_args.args[0].startswith("✅ build (sha) —")
