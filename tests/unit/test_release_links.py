from unittest.mock import patch

import pytest

from calunga_release_watcher import tracker as module
from calunga_release_watcher.tracker import PipelineInfo, PipelineState, PipelineTracker


@pytest.mark.parametrize("host", [
    "https://konflux-ui.apps.stone-prod-p01.wcfb.p1.openshiftapps.com",
    "https://konflux-ui.apps.kflux-prd-rh03.nnv1.p1.openshiftapps.com",
])
@pytest.mark.parametrize("failure", [False, True])
def test_release_messages_link_to_managed_namespace(monkeypatch, host, failure):
    monkeypatch.setattr(module, "KONFLUX_UI_URL", host)
    info = PipelineInfo(sha="sha", sha_short="sha", package_title="package",
                        namespace="tenant", release_pipelinerun="rhtap-releng-tenant/managed-123")
    expected = f"<{host}/ns/rhtap-releng-tenant/pipelineruns/managed-123|Release pipeline>"
    if failure:
        with patch.object(module.threading, "Thread") as thread, patch.object(module, "send_slack_sync") as slack:
            module._handle_failure(None, info, PipelineState.RELEASE_FAILED, "release failed")
            # Capture the failed attempt's link before an asynchronous retry changes it.
            info.release_pipelinerun = "rhtap-releng-tenant/managed-retry"
            thread.call_args.kwargs["target"]()
    else:
        tracker = PipelineTracker()
        tracker._live = True
        with patch.object(module, "_fire_slack") as slack:
            tracker._transition(info, PipelineState.RELEASED)
    assert expected in slack.call_args.args[0]


@pytest.mark.parametrize("url,reference", [
    ("", "managed/one"),
    ("https://ui.example.com", ""),
    ("https://ui.example.com", "one"),
    ("https://ui.example.com", "namespace/"),
])
def test_missing_link_context(monkeypatch, url, reference):
    monkeypatch.setattr(module, "KONFLUX_UI_URL", url)
    info = PipelineInfo(sha="sha", sha_short="sha", package_title="package", release_pipelinerun=reference)
    assert info.release_pipeline_link == ""


def test_build_failures_do_not_link_to_previous_release(monkeypatch):
    monkeypatch.setattr(module, "KONFLUX_UI_URL", "https://ui.example.com")
    info = PipelineInfo(sha="sha", sha_short="sha", package_title="package", release_pipelinerun="managed/old")
    with patch.object(module.threading, "Thread") as thread, patch.object(module, "send_slack_sync") as slack:
        module._handle_failure(None, info, PipelineState.BUILD_FAILED, "build failed")
        thread.call_args.kwargs["target"]()
    assert "Release pipeline" not in slack.call_args.args[0]


@pytest.mark.parametrize("reference", ["managed/recovered", ""])
@pytest.mark.parametrize("failure", [False, True])
def test_terminal_release_rechecks_missing_pipeline_without_waiting(monkeypatch, reference, failure):
    monkeypatch.setattr(module, "KONFLUX_UI_URL", "https://ui.example.com")
    monkeypatch.setattr(module, "IMAGE_METADATA_PROXY_URL", "")
    tracker = PipelineTracker()
    tracker._live = True
    info = tracker.get_or_create("sha", {"metadata": {"name": "build"}})
    body = {"kind": "Release", "metadata": {"name": "release", "namespace": "tenant", "labels": {
        "pac.test.appstudio.openshift.io/sha": "sha",
    }}, "status": {"conditions": [{"type": "Released", "status": "False" if failure else "True", "reason": "Failed" if failure else "Succeeded"}]}}
    resource = {"status": {"managedProcessing": {"pipelineRun": reference}}}
    with patch.object(module, "_read_resource", return_value=resource) as read, patch.object(module, "_fire_slack") as success, patch.object(module.threading, "Thread") as thread, patch.object(module, "send_slack_sync") as failed:
        tracker.on_release(body)
        if failure:
            thread.call_args.kwargs["target"]()
            message = failed.call_args.args[0]
        else:
            message = success.call_args.args[0]
    read.assert_called_once_with("releases", "release", "tenant")
    assert ("<https://ui.example.com/ns/managed/pipelineruns/recovered|Release pipeline>" in message) == bool(reference)
    assert info.state == (PipelineState.RELEASE_FAILED if failure else PipelineState.RELEASED)
