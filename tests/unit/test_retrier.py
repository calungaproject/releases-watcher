from unittest.mock import MagicMock, patch

from calunga_release_watcher.analyzer import FailureAnalysis
from calunga_release_watcher.retrier import (
    _meets_confidence_threshold,
    attempt_retry,
    retry_release,
    retry_test_scenarios,
    should_retry,
)
from calunga_release_watcher.tracker import PipelineInfo, PipelineState

SHA = "abc1234567890def"


def make_pipeline_info(sha=SHA, state=PipelineState.BUILD_RUNNING, **kwargs):
    return PipelineInfo(
        sha=sha,
        sha_short=sha[:7],
        package_title=kwargs.pop("package_title", "test-package"),
        state=state,
        namespace=kwargs.pop("namespace", "calunga-tenant"),
        **kwargs,
    )


def _make_analysis(
    classification="fluke",
    confidence="high",
    failed_scenarios=None,
):
    return FailureAnalysis(
        classification=classification,
        confidence=confidence,
        root_cause="test",
        suggestion="test",
        failed_task="test-task",
        failed_scenarios=failed_scenarios or [],
    )


# ---------------------------------------------------------------------------
# _meets_confidence_threshold
# ---------------------------------------------------------------------------


class TestMeetsConfidenceThreshold:
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    def test_high_meets_medium(self):
        assert _meets_confidence_threshold("high") is True

    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    def test_medium_meets_medium(self):
        assert _meets_confidence_threshold("medium") is True

    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    def test_low_fails_medium(self):
        assert _meets_confidence_threshold("low") is False

    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "low")
    def test_low_meets_low(self):
        assert _meets_confidence_threshold("low") is True

    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "high")
    def test_medium_fails_high(self):
        assert _meets_confidence_threshold("medium") is False


# ---------------------------------------------------------------------------
# should_retry
# ---------------------------------------------------------------------------


class TestShouldRetry:
    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", False)
    def test_disabled(self):
        assert should_retry(_make_analysis(), 0) is False

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    def test_fluke_high_confidence(self):
        assert should_retry(_make_analysis(classification="fluke", confidence="high"), 0) is True

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    def test_real_issue_rejected(self):
        assert should_retry(_make_analysis(classification="real"), 0) is False

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    def test_infra_rejected(self):
        assert should_retry(_make_analysis(classification="infra"), 0) is False

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    def test_max_retries_exhausted(self):
        assert should_retry(_make_analysis(), 3) is False

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "high")
    def test_low_confidence_rejected(self):
        assert should_retry(_make_analysis(confidence="low"), 0) is False


# ---------------------------------------------------------------------------
# attempt_retry
# ---------------------------------------------------------------------------


class TestAttemptRetry:
    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", False)
    def test_retry_disabled_returns_empty(self):
        info = make_pipeline_info()
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.BUILD_FAILED, {}, info)
        assert retried is False
        assert msg == ""

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    def test_build_failure_not_implemented(self):
        info = make_pipeline_info()
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.BUILD_FAILED, {}, info)
        assert retried is False
        assert "not yet implemented" in msg

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    @patch("calunga_release_watcher.retrier.retry_test_scenarios")
    def test_test_failure_triggers_retry(self, mock_retry_tests):
        mock_retry_tests.return_value = ["scenario-a"]
        info = make_pipeline_info(snapshot="snap-1")
        analysis = _make_analysis(failed_scenarios=["scenario-a"])
        retried, msg = attempt_retry(analysis, PipelineState.TESTS_FAILED, {}, info)
        assert retried is True
        assert "scenario-a" in msg
        mock_retry_tests.assert_called_once()

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    @patch("calunga_release_watcher.retrier.retry_release")
    def test_release_failure_triggers_retry(self, mock_retry_release):
        mock_retry_release.return_value = "retry-release-1"
        info = make_pipeline_info(snapshot="snap-1")
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.RELEASE_FAILED, {}, info)
        assert retried is True
        assert "retry-release-1" in msg
        mock_retry_release.assert_called_once()

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 1)
    def test_max_retries_exhausted_message(self):
        info = make_pipeline_info()
        info.build_retry_count = 1
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.BUILD_FAILED, {}, info)
        assert retried is False
        assert "Max retries" in msg
        assert "manual intervention" in msg

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 1)
    def test_test_failure_max_retries_exhausted(self):
        info = make_pipeline_info(snapshot="snap-1")
        info.test_retry_counts["wheel-check"] = 1
        analysis = _make_analysis(failed_scenarios=["wheel-check"])
        retried, msg = attempt_retry(analysis, PipelineState.TESTS_FAILED, {}, info)
        assert retried is False
        assert "Max retries" in msg
        assert "tests" in msg

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    @patch("calunga_release_watcher.retrier.retry_test_scenarios", return_value=[])
    def test_test_failure_retry_returns_empty(self, mock_retry):
        info = make_pipeline_info(snapshot="snap-1")
        analysis = _make_analysis(failed_scenarios=["wheel-check"])
        retried, msg = attempt_retry(analysis, PipelineState.TESTS_FAILED, {}, info)
        assert retried is False
        assert msg == ""

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 1)
    def test_release_failure_max_retries_exhausted(self):
        info = make_pipeline_info(snapshot="snap-1")
        info.release_retry_count = 1
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.RELEASE_FAILED, {}, info)
        assert retried is False
        assert "Max retries" in msg
        assert "release" in msg

    @patch("calunga_release_watcher.retrier.RETRY_ENABLED", True)
    @patch("calunga_release_watcher.retrier.RETRY_CONFIDENCE_THRESHOLD", "medium")
    @patch("calunga_release_watcher.retrier.MAX_RETRIES", 3)
    @patch("calunga_release_watcher.retrier.retry_release", return_value=None)
    def test_release_failure_retry_returns_none(self, mock_retry):
        info = make_pipeline_info(snapshot="snap-1")
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.RELEASE_FAILED, {}, info)
        assert retried is False
        assert msg == ""

    def test_unknown_failure_state_returns_empty(self):
        info = make_pipeline_info()
        analysis = _make_analysis()
        retried, msg = attempt_retry(analysis, PipelineState.BUILD_RUNNING, {}, info)
        assert retried is False
        assert msg == ""


# ---------------------------------------------------------------------------
# retry_test_scenarios (mocked k8s)
# ---------------------------------------------------------------------------


class TestRetryTestScenarios:
    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    def test_single_scenario(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api

        info = make_pipeline_info()
        result = retry_test_scenarios("snap-1", "ns", ["scenario-a"], info)
        assert result == ["scenario-a"]
        assert info.test_retry_counts["scenario-a"] == 1

        patch_body = mock_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert patch_body["metadata"]["labels"]["test.appstudio.openshift.io/run"] == "scenario-a"

    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    def test_multiple_scenarios_uses_all(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api

        info = make_pipeline_info()
        result = retry_test_scenarios("snap-1", "ns", ["s1", "s2"], info)
        assert result == ["s1", "s2"]

        patch_body = mock_api.patch_namespaced_custom_object.call_args[1]["body"]
        assert patch_body["metadata"]["labels"]["test.appstudio.openshift.io/run"] == "all"

    def test_empty_scenarios(self):
        info = make_pipeline_info()
        result = retry_test_scenarios("snap-1", "ns", [], info)
        assert result == []

    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    def test_k8s_exception_returns_empty(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.patch_namespaced_custom_object.side_effect = Exception("forbidden")

        info = make_pipeline_info()
        result = retry_test_scenarios("snap-1", "ns", ["s1"], info)
        assert result == []


# ---------------------------------------------------------------------------
# retry_release (mocked k8s)
# ---------------------------------------------------------------------------


class TestRetryRelease:
    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    @patch("calunga_release_watcher.retrier.RELEASE_PLAN", "calunga")
    def test_creates_release(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "snap-1-retry-abc"},
        }

        original_body = {
            "metadata": {
                "labels": {
                    "appstudio.openshift.io/application": "my-app",
                    "appstudio.openshift.io/component": "my-comp",
                    "pac.test.appstudio.openshift.io/sha": "abc123",
                },
            },
        }
        info = make_pipeline_info()
        result = retry_release(original_body, "snap-1", "ns", info)
        assert result == "snap-1-retry-abc"
        assert info.release_retry_count == 1

        create_body = mock_api.create_namespaced_custom_object.call_args[1]["body"]
        assert create_body["spec"]["snapshot"] == "snap-1"
        assert create_body["spec"]["releasePlan"] == "calunga"

    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    def test_filters_empty_labels(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "retry-1"},
        }

        original_body = {"metadata": {"labels": {}}}
        info = make_pipeline_info()
        retry_release(original_body, "snap-1", "ns", info)

        create_body = mock_api.create_namespaced_custom_object.call_args[1]["body"]
        for v in create_body["metadata"]["labels"].values():
            assert v != ""

    @patch("calunga_release_watcher.retrier.get_k8s_client")
    @patch("calunga_release_watcher.retrier.k8s_client")
    def test_k8s_exception_returns_none(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.create_namespaced_custom_object.side_effect = Exception("conflict")

        info = make_pipeline_info()
        result = retry_release({}, "snap-1", "ns", info)
        assert result is None
