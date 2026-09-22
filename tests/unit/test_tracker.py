from unittest.mock import MagicMock, patch

from calunga_release_watcher.analyzer import FailureAnalysis
from calunga_release_watcher.tracker import (
    PipelineInfo,
    PipelineState,
    PipelineTracker,
    _fire_slack,
    _handle_failure,
    extract_git_info,
    extract_package_title,
    extract_snapshot_name,
    extract_tracking_key,
    get_condition_status,
    get_named_condition,
)

SHA = "abc1234567890def"
SHA_SHORT = "abc123456789"

CONDITION_SUCCEEDED = [{"type": "Succeeded", "status": "True", "reason": "Completed", "message": ""}]
CONDITION_FAILED = [{"type": "Succeeded", "status": "False", "reason": "Failed", "message": "step failed"}]
CONDITION_RUNNING = [{"type": "Succeeded", "status": "Unknown", "reason": "Running", "message": ""}]


def make_body(name="test-plr-1", sha=SHA, kind="PipelineRun", namespace="calunga-tenant",
              labels=None, annotations=None, conditions=None, extra_status=None):
    body = {
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"pac.test.appstudio.openshift.io/sha": sha, **(labels or {})},
            "annotations": annotations or {},
        },
        "status": {},
    }
    if conditions is not None:
        body["status"]["conditions"] = conditions
    if extra_status:
        body["status"].update(extra_status)
    return body


def make_pipeline_info(sha=SHA, state=PipelineState.BUILD_RUNNING, **kwargs):
    return PipelineInfo(
        sha=sha,
        sha_short=sha[:12],
        package_title=kwargs.pop("package_title", "test-package"),
        state=state,
        namespace=kwargs.pop("namespace", "calunga-tenant"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


class TestExtractTrackingKey:
    def test_from_labels(self):
        body = make_body(sha="deadbeef1234")
        assert extract_tracking_key(body) == "deadbeef1234"

    def test_from_annotations(self):
        body = make_body()
        body["metadata"]["labels"] = {}
        body["metadata"]["annotations"] = {
            "pac.test.appstudio.openshift.io/sha": "cafe1234"
        }
        assert extract_tracking_key(body) == "cafe1234"

    def test_missing(self):
        body = {"metadata": {"labels": {}, "annotations": {}}}
        assert extract_tracking_key(body) is None

    def test_empty_metadata(self):
        body = {"metadata": {}}
        assert extract_tracking_key(body) is None

    def test_fallback_to_build_pipelinerun_label(self):
        body = {"metadata": {"labels": {
            "appstudio.openshift.io/build-pipelinerun": "build-plr-abc",
        }, "annotations": {}}}
        assert extract_tracking_key(body) == "build-plr-abc"

    def test_sha_takes_precedence_over_build_plr(self):
        body = {"metadata": {"labels": {
            "pac.test.appstudio.openshift.io/sha": "sha-value",
            "appstudio.openshift.io/build-pipelinerun": "build-plr-abc",
        }, "annotations": {}}}
        assert extract_tracking_key(body) == "sha-value"


class TestExtractPackageTitle:
    def test_from_annotation(self):
        body = make_body(annotations={
            "pac.test.appstudio.openshift.io/sha-title": "Update dependencies"
        })
        assert extract_package_title(body) == "Update dependencies"

    def test_strips_automatic_build_prefix(self):
        body = make_body(annotations={
            "pac.test.appstudio.openshift.io/sha-title": "Automatic build of something"
        })
        assert extract_package_title(body) == "of something"

    def test_takes_first_line(self):
        body = make_body(annotations={
            "pac.test.appstudio.openshift.io/sha-title": "First line\nSecond line"
        })
        assert extract_package_title(body) == "First line"

    def test_fallback_to_image_param_tag(self):
        body = {
            "metadata": {"labels": {}, "annotations": {}},
            "spec": {"params": [
                {"name": "IMAGE", "value": "quay.io/org/image:v1.2.3"},
            ]},
        }
        assert extract_package_title(body) == "v1.2.3"

    def test_fallback_to_component_label(self):
        body = {
            "metadata": {
                "name": "some-plr",
                "labels": {"appstudio.openshift.io/component": "my-component"},
                "annotations": {},
            },
        }
        assert extract_package_title(body) == "my-component"

    def test_fallback_to_resource_name(self):
        body = {
            "metadata": {"name": "my-pipeline-run", "labels": {}, "annotations": {}},
        }
        assert extract_package_title(body) == "my-pipeline-run"


class TestExtractGitInfo:
    def test_from_pipelinerun_results(self):
        body = {
            "metadata": {"labels": {}, "annotations": {}},
            "status": {"results": [
                {"name": "CHAINS-GIT_URL", "value": "https://github.com/org/repo"},
                {"name": "CHAINS-GIT_COMMIT", "value": "abc1234567890"},
            ]},
        }
        url, commit = extract_git_info(body)
        assert url == "https://github.com/org/repo"
        assert commit == "abc1234567890"

    def test_from_snapshot_components(self):
        body = {
            "metadata": {"labels": {}, "annotations": {}},
            "spec": {"components": [
                {"source": {"git": {
                    "url": "https://github.com/org/repo",
                    "revision": "deadbeef",
                }}},
            ]},
        }
        url, commit = extract_git_info(body)
        assert url == "https://github.com/org/repo"
        assert commit == "deadbeef"

    def test_pipelinerun_results_take_precedence(self):
        body = {
            "metadata": {"labels": {}, "annotations": {}},
            "status": {"results": [
                {"name": "CHAINS-GIT_URL", "value": "https://github.com/org/plr-repo"},
                {"name": "CHAINS-GIT_COMMIT", "value": "plr-commit"},
            ]},
            "spec": {"components": [
                {"source": {"git": {
                    "url": "https://github.com/org/snap-repo",
                    "revision": "snap-commit",
                }}},
            ]},
        }
        url, commit = extract_git_info(body)
        assert url == "https://github.com/org/plr-repo"
        assert commit == "plr-commit"

    def test_empty_body(self):
        url, commit = extract_git_info({})
        assert url == ""
        assert commit == ""


class TestExtractSnapshotName:
    def test_present(self):
        body = make_body(labels={"appstudio.openshift.io/snapshot": "snap-1"})
        assert extract_snapshot_name(body) == "snap-1"

    def test_missing(self):
        body = make_body()
        assert extract_snapshot_name(body) == ""


class TestGetConditionStatus:
    def test_succeeded(self):
        body = make_body(conditions=CONDITION_SUCCEEDED)
        status, reason = get_condition_status(body)
        assert status == "True"
        assert reason == "Completed"

    def test_failed(self):
        body = make_body(conditions=CONDITION_FAILED)
        status, reason = get_condition_status(body)
        assert status == "False"
        assert reason == "Failed"

    def test_no_conditions(self):
        body = make_body()
        status, reason = get_condition_status(body)
        assert status is None
        assert reason is None

    def test_empty_conditions(self):
        body = make_body(conditions=[])
        status, reason = get_condition_status(body)
        assert status is None
        assert reason is None


class TestGetNamedCondition:
    def test_finds_matching_type(self):
        body = make_body(conditions=[
            {"type": "AppStudioTestSucceeded", "status": "True", "reason": "Passed"},
            {"type": "AutoReleased", "status": "False", "reason": "Pending"},
        ])
        status, reason = get_named_condition(body, "AppStudioTestSucceeded")
        assert status == "True"
        assert reason == "Passed"

    def test_not_found(self):
        body = make_body(conditions=CONDITION_SUCCEEDED)
        status, reason = get_named_condition(body, "NoSuchCondition")
        assert status is None
        assert reason is None


# ---------------------------------------------------------------------------
# PipelineTracker
# ---------------------------------------------------------------------------


class TestPipelineTrackerGetOrCreate:
    def test_creates_new(self):
        tracker = PipelineTracker()
        body = make_body()
        info = tracker.get_or_create(SHA, body)
        assert info is not None
        assert info.sha == SHA
        assert info.sha_short == SHA_SHORT

    def test_returns_existing(self):
        tracker = PipelineTracker()
        body = make_body()
        info1 = tracker.get_or_create(SHA, body)
        info2 = tracker.get_or_create(SHA, body)
        assert info1 is info2

    def test_returns_none_for_seen_sha_when_live(self):
        tracker = PipelineTracker()
        body = make_body()
        tracker.get_or_create(SHA, body)
        info = tracker._pipelines.pop(SHA)
        tracker.set_live()
        assert tracker.get_or_create(SHA, body) is None


class TestPipelineTrackerTransition:
    def test_same_state_is_noop(self):
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.BUILD_RUNNING)
        tracker._transition(info, PipelineState.BUILD_RUNNING)
        assert info.state == PipelineState.BUILD_RUNNING

    def test_terminal_state_blocks_further_transitions(self):
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.RELEASED)
        tracker._transition(info, PipelineState.BUILD_RUNNING)
        assert info.state == PipelineState.RELEASED

    def test_failed_release_allows_retry_recovery(self):
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.RELEASE_FAILED)
        tracker._transition(info, PipelineState.RELEASE_RETRYING)
        assert info.state == PipelineState.RELEASE_RETRYING

    def test_retrying_allows_failure_for_retry_chain(self):
        # RETRYING → FAILURE is allowed so retry N can trigger retry N+1
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.BUILD_RETRYING)
        tracker._transition(info, PipelineState.BUILD_FAILED)
        assert info.state == PipelineState.BUILD_FAILED

    def test_retrying_blocks_backward_state_rewind(self):
        # Stale replayed events must not rewind RELEASE_RETRYING to earlier stages
        tracker = PipelineTracker()
        for backward in (
            PipelineState.BUILD_RUNNING,
            PipelineState.BUILD_SUCCEEDED,
            PipelineState.SNAPSHOT_CREATED,
            PipelineState.TESTING,
            PipelineState.TESTS_PASSED,
            PipelineState.RELEASING,
        ):
            info = make_pipeline_info(state=PipelineState.RELEASE_RETRYING)
            tracker._transition(info, backward)
            assert info.state == PipelineState.RELEASE_RETRYING, f"should block RELEASE_RETRYING → {backward}"

    def test_retrying_allows_released(self):
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.RELEASE_RETRYING)
        tracker._transition(info, PipelineState.RELEASED)
        assert info.state == PipelineState.RELEASED

    def test_normal_transition(self):
        tracker = PipelineTracker()
        info = make_pipeline_info(state=PipelineState.BUILD_RUNNING)
        tracker._transition(info, PipelineState.BUILD_SUCCEEDED)
        assert info.state == PipelineState.BUILD_SUCCEEDED


class TestPipelineTrackerSetLive:
    def test_filters_terminal_and_stale(self):
        tracker = PipelineTracker()
        body_released = make_body(name="plr-released", sha="sha1")
        body_stale = make_body(name="plr-stale", sha="sha2")
        body_active = make_body(name="plr-active", sha="sha3")

        info_released = tracker.get_or_create("sha1", body_released)
        info_released.state = PipelineState.RELEASED
        info_released.build_pipelinerun = "plr-released"

        info_stale = tracker.get_or_create("sha2", body_stale)
        # no build_pipelinerun set — stale

        info_active = tracker.get_or_create("sha3", body_active)
        info_active.state = PipelineState.BUILD_RUNNING
        info_active.build_pipelinerun = "plr-active"

        tracker.set_live()

        assert "sha1" not in tracker._pipelines
        assert "sha2" not in tracker._pipelines
        assert "sha3" in tracker._pipelines

    def test_prunes_snapshot_index(self):
        tracker = PipelineTracker()
        body_active = make_body(name="plr-active", sha="active-sha")
        info = tracker.get_or_create("active-sha", body_active)
        info.build_pipelinerun = "plr-active"
        tracker._snapshot_index["snap-active"] = "active-sha"
        tracker._snapshot_index["snap-stale"] = "gone-sha"

        tracker.set_live()

        assert "snap-active" in tracker._snapshot_index
        assert "snap-stale" not in tracker._snapshot_index


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


class TestFireSlack:
    @patch("calunga_release_watcher.tracker.threading.Thread")
    def test_starts_daemon_thread(self, mock_thread_cls):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread
        _fire_slack("hello", "ts123")
        mock_thread_cls.assert_called_once()
        mock_thread.start.assert_called_once()


class TestOnBuildPipelineRun:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_running(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="build-1")
        tracker.on_build_pipelinerun(body)
        info = tracker.get(SHA)
        assert info is not None
        assert info.state == PipelineState.BUILD_RUNNING
        assert info.build_pipelinerun == "build-1"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_succeeded(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="build-1", conditions=CONDITION_SUCCEEDED)
        tracker.on_build_pipelinerun(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.BUILD_SUCCEEDED

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failed_when_live(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="build-1", conditions=CONDITION_FAILED)
        tracker.on_build_pipelinerun(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.BUILD_FAILED
        mock_handle.assert_called_once()

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_no_sha_uses_plr_name_as_key(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="my-build-plr")
        body["metadata"]["labels"] = {}
        tracker.on_build_pipelinerun(body)
        info = tracker.get("my-build-plr")
        assert info is not None
        assert info.build_pipelinerun == "my-build-plr"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_pruned_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add(SHA)  # seen but pruned from _pipelines
        tracker.on_build_pipelinerun(make_body(name="build-1"))
        assert tracker.get(SHA) is None

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failure_deduped_on_second_call(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="build-1", conditions=CONDITION_FAILED)
        tracker.on_build_pipelinerun(body)
        assert mock_handle.call_count == 1
        # Simulate retry: unblock the terminal state
        tracker.get(SHA).state = PipelineState.BUILD_RETRYING
        # Same event replayed — dedup must suppress the second _handle_failure
        tracker.on_build_pipelinerun(body)
        assert mock_handle.call_count == 1

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_succeeded_populates_git_info(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="build-1", conditions=CONDITION_SUCCEEDED, extra_status={
            "results": [
                {"name": "CHAINS-GIT_URL", "value": "https://github.com/org/repo"},
                {"name": "CHAINS-GIT_COMMIT", "value": "abc123"},
            ]
        })
        tracker.on_build_pipelinerun(body)
        info = tracker.get(SHA)
        assert info.git_url == "https://github.com/org/repo"
        assert info.git_commit == "abc123"


class TestOnSnapshot:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_snapshot_created(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="snap-1", kind="Snapshot")
        tracker.on_snapshot(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.SNAPSHOT_CREATED
        assert info.snapshot == "snap-1"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_snapshot_populates_index(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="snap-1", kind="Snapshot")
        tracker.on_snapshot(body)
        assert tracker._snapshot_index.get("snap-1") == SHA

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_tests_passed(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="snap-1", kind="Snapshot", conditions=[
            {"type": "AppStudioTestSucceeded", "status": "True", "reason": "Passed"},
        ])
        tracker.on_snapshot(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.TESTS_PASSED

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_tests_failed_when_live(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="snap-1", kind="Snapshot", conditions=[
            {"type": "AppStudioTestSucceeded", "status": "False", "reason": "TestFailed"},
        ])
        tracker.on_snapshot(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.TESTS_FAILED
        mock_handle.assert_called_once()

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_no_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(kind="Snapshot")
        body["metadata"]["labels"] = {}
        tracker.on_snapshot(body)
        assert len(tracker._pipelines) == 0

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_pruned_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add(SHA)
        tracker.on_snapshot(make_body(name="snap-1", kind="Snapshot"))
        assert tracker.get(SHA) is None

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_auto_released_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="snap-1", kind="Snapshot", conditions=[
            {"type": "AppStudioTestSucceeded", "status": "True", "reason": "Passed"},
            {"type": "AutoReleased", "status": "True", "reason": "Released"},
        ])
        tracker.on_snapshot(body)
        # test_status=True AND release_status=True → pass (no transition)
        info = tracker.get(SHA)
        assert info.state == PipelineState.BUILD_RUNNING

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failure_deduped_on_second_call(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="snap-1", kind="Snapshot", conditions=[
            {"type": "AppStudioTestSucceeded", "status": "False", "reason": "TestFailed"},
        ])
        tracker.on_snapshot(body)
        assert mock_handle.call_count == 1
        tracker.get(SHA).state = PipelineState.TESTS_RETRYING
        tracker.on_snapshot(body)
        assert mock_handle.call_count == 1


class TestOnTestPipelineRun:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_test_started_transitions_to_testing(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="build-1")
        tracker.on_build_pipelinerun(body)
        info = tracker.get(SHA)
        info.state = PipelineState.SNAPSHOT_CREATED

        test_body = make_body(
            name="test-1",
            labels={"test.appstudio.openshift.io/scenario": "my-scenario"},
        )
        tracker.on_test_pipelinerun(test_body)
        assert info.state == PipelineState.TESTING

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_tracks_test_pipelinerun_status(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="test-1", conditions=CONDITION_SUCCEEDED)
        tracker.on_test_pipelinerun(body)
        info = tracker.get(SHA)
        assert "test-1" in info.test_pipelineruns
        assert info.test_pipelineruns["test-1"] == "True"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_no_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="test-1")
        body["metadata"]["labels"] = {}
        tracker.on_test_pipelinerun(body)
        assert len(tracker._pipelines) == 0

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_pruned_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add(SHA)
        tracker.on_test_pipelinerun(make_body(name="test-1"))
        assert tracker.get(SHA) is None

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_test_passed_logs_when_live(self, mock_handle, caplog):
        import logging
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(
            name="test-1",
            labels={"test.appstudio.openshift.io/scenario": "wheel-check"},
            conditions=CONDITION_SUCCEEDED,
        )
        with caplog.at_level(logging.INFO, logger="calunga_release_watcher.tracker"):
            tracker.on_test_pipelinerun(body)
        assert any("Test passed" in r.message for r in caplog.records)

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_test_failed_logs_warning_when_live(self, mock_handle, caplog):
        import logging
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(
            name="test-1",
            labels={"test.appstudio.openshift.io/scenario": "wheel-check"},
            conditions=CONDITION_FAILED,
        )
        with caplog.at_level(logging.WARNING, logger="calunga_release_watcher.tracker"):
            tracker.on_test_pipelinerun(body)
        assert any("Test FAILED" in r.message for r in caplog.records)


class TestOnRelease:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_releasing(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="rel-1", kind="Release", conditions=[
            {"type": "Released", "status": "Unknown", "reason": "Progressing"},
        ])
        tracker.on_release(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASING
        assert info.release == "rel-1"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_released(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="rel-1", kind="Release", conditions=[
            {"type": "Released", "status": "True", "reason": "Succeeded"},
        ])
        tracker.on_release(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASED

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_release_failed_when_live(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="rel-1", kind="Release", conditions=[
            {"type": "Released", "status": "False", "reason": "Error"},
        ])
        tracker.on_release(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASE_FAILED
        mock_handle.assert_called_once()

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_no_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="rel-1", kind="Release")
        body["metadata"]["labels"] = {}
        tracker.on_release(body)
        assert len(tracker._pipelines) == 0

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_pruned_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add(SHA)
        tracker.on_release(make_body(name="rel-1", kind="Release"))
        assert tracker.get(SHA) is None

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_sets_pipelinerun_from_managed_processing(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="rel-1", kind="Release", extra_status={
            "managedProcessing": {"pipelineRun": "rhtap-releng-tenant/managed-abc"},
        })
        tracker.on_release(body)
        info = tracker.get(SHA)
        assert info.release_pipelinerun == "rhtap-releng-tenant/managed-abc"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failure_deduped_on_second_call(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="rel-1", kind="Release", conditions=[
            {"type": "Released", "status": "False", "reason": "Error"},
        ])
        tracker.on_release(body)
        assert mock_handle.call_count == 1
        tracker.get(SHA).state = PipelineState.RELEASE_RETRYING
        tracker.on_release(body)
        assert mock_handle.call_count == 1

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_manual_rerelease_recovers_failed_pipeline(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        failed = make_body(name="rel-failed", kind="Release", conditions=[
            {"type": "Released", "status": "False", "reason": "Error"},
        ])
        tracker.on_release(failed)
        assert tracker.get(SHA).state == PipelineState.RELEASE_FAILED

        retry_started = make_body(name="rel-retry", kind="Release", conditions=[
            {"type": "Released", "status": "Unknown", "reason": "Progressing"},
        ])
        tracker.on_release(retry_started)
        assert tracker.get(SHA).state == PipelineState.RELEASE_RETRYING

        retry_succeeded = make_body(name="rel-retry", kind="Release", conditions=[
            {"type": "Released", "status": "True", "reason": "Succeeded"},
        ])
        tracker.on_release(retry_succeeded)
        assert tracker.get(SHA).state == PipelineState.RELEASED

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_release_found_via_snapshot_index(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add("build-plr-name")
        tracker._pipelines["build-plr-name"] = PipelineInfo(
            sha="build-plr-name",
            sha_short="build-plr-n",
            package_title="pnc-import",
            state=PipelineState.RELEASING,
            namespace="lightwell-poc-tenant",
            build_pipelinerun="build-plr-name",
        )
        tracker._snapshot_index["snap-xyz"] = "build-plr-name"

        body = {
            "metadata": {
                "name": "rel-2",
                "namespace": "lightwell-poc-tenant",
                "labels": {
                    "release.appstudio.openshift.io/snapshot": "snap-xyz",
                },
                "annotations": {},
            },
            "status": {"conditions": [
                {"type": "Released", "status": "True", "reason": "Succeeded"},
            ]},
        }
        tracker.on_release(body)
        info = tracker._pipelines.get("build-plr-name")
        assert info is not None
        assert info.state == PipelineState.RELEASED


class TestOnReleasePipelineRun:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_succeeded(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(
            name="managed-plr-1",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_SUCCEEDED,
        )
        tracker.on_release_pipelinerun(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASED
        assert info.release_pipelinerun == "rhtap-releng-tenant/managed-plr-1"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failed_when_live(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(
            name="managed-plr-1",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_FAILED,
        )
        tracker.on_release_pipelinerun(body)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASE_FAILED
        mock_handle.assert_called_once()

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_no_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        body = make_body(name="managed-plr-1", namespace="rhtap-releng-tenant")
        body["metadata"]["labels"] = {}
        tracker.on_release_pipelinerun(body)
        assert len(tracker._pipelines) == 0

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_pruned_sha_is_noop(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        tracker._seen_keys.add(SHA)
        tracker.on_release_pipelinerun(
            make_body(name="managed-plr-1", namespace="rhtap-releng-tenant")
        )
        assert tracker.get(SHA) is None

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_started_logs_when_live(self, mock_handle, caplog):
        import logging
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(name="managed-plr-1", namespace="rhtap-releng-tenant")
        with caplog.at_level(logging.INFO, logger="calunga_release_watcher.tracker"):
            tracker.on_release_pipelinerun(body)
        assert any("Release PipelineRun started" in r.message for r in caplog.records)

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_failure_deduped_on_second_call(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        body = make_body(
            name="managed-plr-1",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_FAILED,
        )
        tracker.on_release_pipelinerun(body)
        assert mock_handle.call_count == 1
        tracker.get(SHA).state = PipelineState.RELEASE_RETRYING
        tracker.on_release_pipelinerun(body)
        assert mock_handle.call_count == 1

    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="")
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_manual_retry_success_replies_in_failure_thread(
        self, mock_handle, mock_slack,
    ):
        tracker = PipelineTracker()
        tracker._live = True
        failed = make_body(
            name="managed-nz6s5",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_FAILED,
        )
        tracker.on_release_pipelinerun(failed)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASE_FAILED
        info.failure_thread_ts = "1234.5678"

        retry_started = make_body(
            name="managed-s2bgf",
            namespace="rhtap-releng-tenant",
        )
        tracker.on_release_pipelinerun(retry_started)
        assert info.state == PipelineState.RELEASE_RETRYING

        retry_succeeded = make_body(
            name="managed-s2bgf",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_SUCCEEDED,
        )
        tracker.on_release_pipelinerun(retry_succeeded)

        assert info.state == PipelineState.RELEASED
        mock_slack.assert_called_once()
        message, thread_ts = mock_slack.call_args[0]
        assert "pipeline complete" in message
        assert "managed-s2bgf" in message
        assert thread_ts == "1234.5678"

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_stale_old_attempt_does_not_reopen_failed_retry(self, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        original_failed = make_body(
            name="managed-original",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_FAILED,
        )
        tracker.on_release_pipelinerun(original_failed)

        retry_failed = make_body(
            name="managed-retry",
            namespace="rhtap-releng-tenant",
            conditions=CONDITION_FAILED,
        )
        tracker.on_release_pipelinerun(retry_failed)
        info = tracker.get(SHA)
        assert info.state == PipelineState.RELEASE_FAILED

        stale_original = make_body(
            name="managed-original",
            namespace="rhtap-releng-tenant",
        )
        tracker.on_release_pipelinerun(stale_original)
        assert info.state == PipelineState.RELEASE_FAILED


# ---------------------------------------------------------------------------
# Non-PAC (pnc-import style) flow
# ---------------------------------------------------------------------------


class TestNonPacFlow:
    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_full_non_pac_flow_via_snapshot_index(self, mock_handle):
        """
        pnc-import has no PAC labels. Build PLR is tracked under its own name.
        Snapshot links back via _snapshot_index. Release succeeds.
        """
        tracker = PipelineTracker()
        tracker._live = True

        build_body = {
            "metadata": {
                "name": "pnc-import-build-abc",
                "namespace": "lightwell-poc-tenant",
                "labels": {"appstudio.openshift.io/application": "pnc-import"},
                "annotations": {},
            },
            "status": {"conditions": CONDITION_SUCCEEDED},
        }
        tracker.on_build_pipelinerun(build_body)
        info = tracker._pipelines.get("pnc-import-build-abc")
        assert info is not None
        assert info.state == PipelineState.BUILD_SUCCEEDED

        snap_body = {
            "metadata": {
                "name": "snap-pnc-123",
                "namespace": "lightwell-poc-tenant",
                "labels": {
                    "appstudio.openshift.io/build-pipelinerun": "pnc-import-build-abc",
                },
                "annotations": {},
            },
            "status": {},
        }
        tracker.on_snapshot(snap_body)
        assert tracker._snapshot_index.get("snap-pnc-123") == "pnc-import-build-abc"
        assert info.snapshot == "snap-pnc-123"

        release_body = {
            "metadata": {
                "name": "rel-pnc-1",
                "namespace": "lightwell-poc-tenant",
                "labels": {
                    "release.appstudio.openshift.io/snapshot": "snap-pnc-123",
                },
                "annotations": {},
            },
            "status": {"conditions": [
                {"type": "Released", "status": "True", "reason": "Succeeded"},
            ]},
        }
        tracker.on_release(release_body)
        assert info.state == PipelineState.RELEASED

    @patch("calunga_release_watcher.tracker._handle_failure")
    def test_release_pipelinerun_found_via_snapshot_label(self, mock_handle):
        """Release PLR carries snapshot label — resolved via _snapshot_index."""
        tracker = PipelineTracker()
        tracker._live = True
        tracker._pipelines["build-plr-xyz"] = PipelineInfo(
            sha="build-plr-xyz",
            sha_short="build-plr-x",
            package_title="pnc-import",
            state=PipelineState.RELEASING,
            namespace="lightwell-poc-tenant",
            build_pipelinerun="build-plr-xyz",
            snapshot="snap-abc",
        )
        tracker._snapshot_index["snap-abc"] = "build-plr-xyz"
        tracker._seen_keys.add("build-plr-xyz")

        plr_body = {
            "metadata": {
                "name": "managed-plr-1",
                "namespace": "rhtap-releng-tenant",
                "labels": {
                    "appstudio.openshift.io/snapshot": "snap-abc",
                },
                "annotations": {},
            },
            "status": {"conditions": CONDITION_SUCCEEDED},
        }
        tracker.on_release_pipelinerun(plr_body)
        info = tracker._pipelines["build-plr-xyz"]
        assert info.state == PipelineState.RELEASED


# ---------------------------------------------------------------------------
# Live transition notifications
# ---------------------------------------------------------------------------


class TestTransitionLiveNotifications:
    @patch("calunga_release_watcher.tracker._handle_failure")
    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="")
    @patch("calunga_release_watcher.tracker._fire_slack")
    def test_released_sends_slack_new_thread(self, mock_fire, mock_send, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        info = make_pipeline_info(state=PipelineState.RELEASING, release_pipelinerun="ns/plr-1")
        tracker._transition(info, PipelineState.RELEASED, "done")
        assert info.state == PipelineState.RELEASED
        mock_fire.assert_called_once()
        assert "pipeline complete" in mock_fire.call_args[0][0]

    @patch("calunga_release_watcher.tracker._handle_failure")
    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="")
    def test_released_replies_in_failure_thread(self, mock_send, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        info = make_pipeline_info(
            state=PipelineState.RELEASING,
            release_pipelinerun="ns/plr-1",
            failure_thread_ts="1234.5678",
        )
        tracker._transition(info, PipelineState.RELEASED, "done")
        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == "1234.5678"

    @patch("calunga_release_watcher.tracker._handle_failure")
    @patch("calunga_release_watcher.tracker.send_slack_sync")
    @patch("calunga_release_watcher.tracker._fire_slack")
    def test_non_terminal_no_slack(self, mock_fire, mock_send, mock_handle):
        tracker = PipelineTracker()
        tracker._live = True
        info = make_pipeline_info(state=PipelineState.BUILD_RUNNING)
        tracker._transition(info, PipelineState.BUILD_SUCCEEDED, "ok")
        mock_fire.assert_not_called()
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_failure (call _worker synchronously)
# ---------------------------------------------------------------------------


class TestHandleFailure:
    @patch("calunga_release_watcher.tracker.threading.Thread")
    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="ts-123")
    @patch("calunga_release_watcher.tracker.analyze_failure")
    @patch("calunga_release_watcher.retrier.attempt_retry", return_value=(True, "retrying..."))
    def test_analysis_success_with_retry(self, mock_retry, mock_analyze, mock_slack, mock_thread):
        analysis = FailureAnalysis(
            classification="fluke", confidence="high",
            root_cause="timeout", suggestion="retry",
            failed_task="build", failed_scenarios=[],
        )
        mock_analyze.return_value = analysis

        info = make_pipeline_info(state=PipelineState.BUILD_FAILED)
        body = make_body()

        _handle_failure(body, info, PipelineState.BUILD_FAILED, "build failed")

        mock_thread.assert_called_once()
        worker = mock_thread.call_args[1]["target"]
        worker()

        assert info.failure_thread_ts == "ts-123"
        mock_analyze.assert_called_once()
        mock_retry.assert_called_once()

    @patch("calunga_release_watcher.tracker.threading.Thread")
    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="")
    @patch("calunga_release_watcher.tracker.analyze_failure", side_effect=Exception("AI down"))
    def test_analysis_exception_still_sends_slack(self, mock_analyze, mock_slack, mock_thread):
        info = make_pipeline_info(state=PipelineState.BUILD_FAILED)
        body = make_body()

        _handle_failure(body, info, PipelineState.BUILD_FAILED, "build failed")
        worker = mock_thread.call_args[1]["target"]
        worker()

        mock_slack.assert_called_once()
        assert "build failed" in mock_slack.call_args[0][0]

    @patch("calunga_release_watcher.tracker.threading.Thread")
    @patch("calunga_release_watcher.tracker.send_slack_sync", return_value="ts-456")
    def test_body_none_skips_analysis(self, mock_slack, mock_thread):
        info = make_pipeline_info(state=PipelineState.BUILD_FAILED)

        _handle_failure(None, info, PipelineState.BUILD_FAILED, "build failed")
        worker = mock_thread.call_args[1]["target"]
        worker()

        mock_slack.assert_called_once()
        assert info.failure_thread_ts == "ts-456"
