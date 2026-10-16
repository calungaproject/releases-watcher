from unittest.mock import Mock

import pytest

from calunga_release_watcher import handlers
from calunga_release_watcher.config import LBL_BUILD_EVENT_TYPE, LBL_TEST_EVENT_TYPE
from calunga_release_watcher.tracker import PipelineTracker


@pytest.fixture
def tracker(monkeypatch):
    tracker = PipelineTracker()
    tracker.set_live()
    monkeypatch.setattr(handlers, "tracker", tracker)
    monkeypatch.setattr(handlers, "APPLICATIONS", {"calunga-v2-index-main"})
    monkeypatch.setattr(handlers, "WATCH_EVENT_TYPE", "push")
    return tracker


@pytest.mark.parametrize("handler,event_key", [
    (handlers.on_build_pipelinerun, LBL_BUILD_EVENT_TYPE),
    (handlers.on_test_pipelinerun, LBL_TEST_EVENT_TYPE),
    (handlers.on_snapshot, LBL_TEST_EVENT_TYPE),
])
@pytest.mark.parametrize("event_type", ["pull_request", "push", None])
@pytest.mark.parametrize("location", ["labels", "annotations"])
def test_event_filter_before_tracking(tracker, mocker, handler, event_key, event_type, location):
    failure = mocker.patch("calunga_release_watcher.tracker._handle_failure")
    body = {
        "metadata": {
            "name": "pipeline-resource",
            "namespace": "calunga-tenant",
            "labels": {
                "appstudio.openshift.io/application": "calunga-v2-index-main",
                "pac.test.appstudio.openshift.io/sha": "same-commit",
            },
            "annotations": {},
        },
        "status": {"conditions": [
            {"type": "Succeeded", "status": "False", "reason": "Failed"},
            {"type": "AppStudioTestSucceeded", "status": "False"},
        ]},
    }
    if event_type is not None:
        body["metadata"][location][event_key] = event_type
    handler(body)
    assert (tracker.get("same-commit") is not None) == (event_type == "push")
    assert failure.call_count == int(event_type == "push" and handler != handlers.on_test_pipelinerun)
    # PR events for the same SHA must not alter existing on-merge state either.
    if event_type == "push":
        body["metadata"][location][event_key] = "pull_request"
        body["metadata"]["name"] = "pr-resource"
        info = tracker.get("same-commit")
        prior = repr(info)
        handler(body)
        assert repr(info) == prior


def test_unconfigured_filter_preserves_other_instances(monkeypatch):
    monkeypatch.setattr(handlers, "WATCH_EVENT_TYPE", "")
    assert handlers._event_matches({"metadata": {}}, LBL_BUILD_EVENT_TYPE)


@pytest.mark.parametrize("handler", [handlers.on_release, handlers.on_release_pipelinerun])
def test_releases_without_event_metadata_are_forwarded(tracker, monkeypatch, handler):
    callback = Mock()
    monkeypatch.setattr(tracker, handler.__name__, callback)
    body = {"metadata": {"labels": {"appstudio.openshift.io/application": "calunga-v2-index-main"}}}
    handler(body)
    callback.assert_called_once_with(body)
