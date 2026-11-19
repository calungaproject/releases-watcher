import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from calunga_release_watcher.analyzer import (
    FailureAnalysis,
    _build_context,
    _build_pipelinerun_context,
    _build_release_failure_context,
    _call_claude,
    _get_child_taskruns,
    _get_failed_taskruns,
    _get_pod_logs,
    _get_test_pipelineruns_for_snapshot,
    _resolve_managed_pipelinerun_ref,
    _truncate_context,
    analyze_failure,
    extract_failed_scenarios,
    format_analysis,
)
from calunga_release_watcher.tracker import PipelineInfo, PipelineState


# ---------------------------------------------------------------------------
# extract_failed_scenarios
# ---------------------------------------------------------------------------


class TestExtractFailedScenarios:
    def test_extracts_failed(self):
        body = {
            "kind": "Snapshot",
            "metadata": {
                "annotations": {
                    "test.appstudio.openshift.io/status": json.dumps([
                        {"scenario": "scenario-a", "status": "TestFailed"},
                        {"scenario": "scenario-b", "status": "TestPassed"},
                        {"scenario": "scenario-c", "status": "EnvironmentProvisionError"},
                    ]),
                },
            },
        }
        result = extract_failed_scenarios(body)
        assert result == ["scenario-a", "scenario-c"]

    def test_not_snapshot(self):
        body = {"kind": "PipelineRun", "metadata": {"annotations": {}}}
        assert extract_failed_scenarios(body) == []

    def test_missing_annotation(self):
        body = {"kind": "Snapshot", "metadata": {"annotations": {}}}
        assert extract_failed_scenarios(body) == []

    def test_bad_json(self):
        body = {
            "kind": "Snapshot",
            "metadata": {"annotations": {"test.appstudio.openshift.io/status": "not-json"}},
        }
        assert extract_failed_scenarios(body) == []

    def test_empty_list(self):
        body = {
            "kind": "Snapshot",
            "metadata": {"annotations": {"test.appstudio.openshift.io/status": "[]"}},
        }
        assert extract_failed_scenarios(body) == []

    def test_deployment_error(self):
        body = {
            "kind": "Snapshot",
            "metadata": {
                "annotations": {
                    "test.appstudio.openshift.io/status": json.dumps([
                        {"scenario": "deploy-test", "status": "DeploymentError"},
                    ]),
                },
            },
        }
        assert extract_failed_scenarios(body) == ["deploy-test"]


# ---------------------------------------------------------------------------
# _truncate_context
# ---------------------------------------------------------------------------


class TestTruncateContext:
    def test_within_budget(self):
        parts = [("Label A", "short content"), ("Label B", "more content")]
        result = _truncate_context(parts)
        assert "Label A" in result
        assert "Label B" in result
        assert "short content" in result

    def test_truncates_large_content(self):
        large = "x" * 100_000
        parts = [("Big", large)]
        result = _truncate_context(parts)
        assert "[... truncated" in result
        assert len(result) < 100_000

    def test_budget_exhausted_marks_truncated(self):
        parts = [("A", "x" * 50_000), ("B", "y" * 50_000)]
        result = _truncate_context(parts)
        assert "A" in result
        assert "[TRUNCATED]" in result


# ---------------------------------------------------------------------------
# format_analysis
# ---------------------------------------------------------------------------


class TestFormatAnalysis:
    def test_fluke_format(self):
        analysis = FailureAnalysis(
            classification="fluke",
            confidence="high",
            root_cause="Network timeout pulling image",
            suggestion="Retry the pipeline",
            failed_task="build-container",
        )
        result = format_analysis(analysis)
        assert "AI Analysis" in result
        assert "fluke" in result
        assert "Network timeout" in result
        assert "Retry the pipeline" in result

    def test_real_format(self):
        analysis = FailureAnalysis(
            classification="real",
            confidence="medium",
            root_cause="Compilation error",
            suggestion="Fix syntax",
            failed_task="compile",
        )
        result = format_analysis(analysis)
        assert "real" in result

    def test_suggestion_none_omitted(self):
        analysis = FailureAnalysis(
            classification="unknown",
            confidence="low",
            root_cause="Unclear",
            suggestion="none",
            failed_task="unknown",
        )
        result = format_analysis(analysis)
        assert "Suggestion" not in result


# ---------------------------------------------------------------------------
# _call_claude
# ---------------------------------------------------------------------------


class TestCallClaude:
    @patch("calunga_release_watcher.analyzer.GOOGLE_CLOUD_PROJECT", "test-project")
    @patch("calunga_release_watcher.analyzer.AnthropicVertex")
    def test_returns_analysis(self, mock_vertex_cls):
        mock_client = MagicMock()
        mock_vertex_cls.return_value = mock_client

        response_json = json.dumps({
            "classification": "fluke",
            "confidence": "high",
            "root_cause": "Image pull timeout",
            "suggestion": "Retry",
            "failed_task": "build-container",
            "failed_scenarios": [],
        })
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_json)]
        mock_client.messages.create.return_value = mock_response

        result = _call_claude("some context", "build_failed")
        assert result is not None
        assert result.classification == "fluke"
        assert result.confidence == "high"

        mock_vertex_cls.assert_called_once_with(
            project_id="test-project",
            region="global",
        )

    @patch("calunga_release_watcher.analyzer.GOOGLE_CLOUD_PROJECT", "")
    def test_skips_when_no_project(self):
        result = _call_claude("context", "build_failed")
        assert result is None


# ---------------------------------------------------------------------------
# _get_failed_taskruns (pure — no mock needed)
# ---------------------------------------------------------------------------


class TestGetFailedTaskruns:
    def test_identifies_failed(self):
        taskruns = [
            {"status": {"conditions": [{"type": "Succeeded", "status": "False"}]}},
            {"status": {"conditions": [{"type": "Succeeded", "status": "True"}]}},
            {"status": {"conditions": [{"type": "Succeeded", "status": "False"}]}},
        ]
        result = _get_failed_taskruns(taskruns)
        assert len(result) == 2

    def test_no_failures(self):
        taskruns = [
            {"status": {"conditions": [{"type": "Succeeded", "status": "True"}]}},
        ]
        assert _get_failed_taskruns(taskruns) == []

    def test_empty_list(self):
        assert _get_failed_taskruns([]) == []

    def test_no_conditions(self):
        taskruns = [{"status": {}}]
        assert _get_failed_taskruns(taskruns) == []


# ---------------------------------------------------------------------------
# _get_child_taskruns (mocked k8s)
# ---------------------------------------------------------------------------


class TestGetChildTaskruns:
    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_fetches_taskruns(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.get_namespaced_custom_object.return_value = {"metadata": {"name": "tr-1"}}

        body = {
            "status": {
                "childReferences": [
                    {"kind": "TaskRun", "name": "tr-1"},
                ],
            },
        }
        result = _get_child_taskruns(body, "ns")
        assert len(result) == 1
        assert result[0]["metadata"]["name"] == "tr-1"

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_skips_non_taskrun_refs(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api

        body = {
            "status": {
                "childReferences": [
                    {"kind": "Run", "name": "run-1"},
                ],
            },
        }
        result = _get_child_taskruns(body, "ns")
        assert result == []
        mock_api.get_namespaced_custom_object.assert_not_called()

    def test_no_child_refs(self):
        body = {"status": {}}
        assert _get_child_taskruns(body, "ns") == []

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_handles_api_exception(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.get_namespaced_custom_object.side_effect = Exception("api error")

        body = {"status": {"childReferences": [{"kind": "TaskRun", "name": "tr-1"}]}}
        result = _get_child_taskruns(body, "ns")
        assert result == []


# ---------------------------------------------------------------------------
# _get_test_pipelineruns_for_snapshot (mocked k8s)
# ---------------------------------------------------------------------------


class TestGetTestPipelinerunsForSnapshot:
    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_returns_items(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "plr-1"}}, {"metadata": {"name": "plr-2"}}],
        }
        result = _get_test_pipelineruns_for_snapshot("snap-1", "ns")
        assert len(result) == 2

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_handles_exception(self, mock_k8s_mod, mock_get_client):
        mock_api = MagicMock()
        mock_k8s_mod.CustomObjectsApi.return_value = mock_api
        mock_api.list_namespaced_custom_object.side_effect = Exception("api error")
        result = _get_test_pipelineruns_for_snapshot("snap-1", "ns")
        assert result == []


# ---------------------------------------------------------------------------
# _get_pod_logs (mocked k8s)
# ---------------------------------------------------------------------------


class TestGetPodLogs:
    def test_no_pod_name(self):
        taskrun = {"status": {}}
        assert _get_pod_logs(taskrun, "ns", 100) == {}

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_returns_logs(self, mock_k8s_mod, mock_get_client):
        mock_core = MagicMock()
        mock_k8s_mod.CoreV1Api.return_value = mock_core

        container = SimpleNamespace(name="step-build")
        pod = SimpleNamespace(
            spec=SimpleNamespace(containers=[container], init_containers=[]),
        )
        mock_core.read_namespaced_pod.return_value = pod
        mock_core.read_namespaced_pod_log.return_value = "build output here"

        taskrun = {"status": {"podName": "pod-1"}}
        result = _get_pod_logs(taskrun, "ns", 200)
        assert "step-build" in result
        assert result["step-build"] == "build output here"

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_pod_read_failure(self, mock_k8s_mod, mock_get_client):
        mock_core = MagicMock()
        mock_k8s_mod.CoreV1Api.return_value = mock_core
        mock_core.read_namespaced_pod.side_effect = Exception("not found")

        taskrun = {"status": {"podName": "pod-1"}}
        assert _get_pod_logs(taskrun, "ns", 100) == {}

    @patch("calunga_release_watcher.analyzer.get_k8s_client")
    @patch("calunga_release_watcher.analyzer.k8s_client")
    def test_empty_log_skipped(self, mock_k8s_mod, mock_get_client):
        mock_core = MagicMock()
        mock_k8s_mod.CoreV1Api.return_value = mock_core

        container = SimpleNamespace(name="step-build")
        pod = SimpleNamespace(
            spec=SimpleNamespace(containers=[container], init_containers=[]),
        )
        mock_core.read_namespaced_pod.return_value = pod
        mock_core.read_namespaced_pod_log.return_value = ""

        taskrun = {"status": {"podName": "pod-1"}}
        result = _get_pod_logs(taskrun, "ns", 100)
        assert result == {}


# ---------------------------------------------------------------------------
# _build_pipelinerun_context (mocked helpers)
# ---------------------------------------------------------------------------


class TestBuildPipelinerunContext:
    @patch("calunga_release_watcher.analyzer._get_pod_logs", return_value={})
    @patch("calunga_release_watcher.analyzer._get_child_taskruns")
    def test_adds_condition_and_taskruns(self, mock_child, mock_logs):
        failed_tr = {
            "metadata": {"name": "tr-fail", "namespace": "ns"},
            "status": {"conditions": [{"type": "Succeeded", "status": "False", "reason": "Failed", "message": "err"}]},
        }
        mock_child.return_value = [failed_tr]

        plr = {
            "metadata": {"name": "plr-1"},
            "status": {
                "conditions": [{"status": "False", "reason": "Failed", "message": "pipeline failed"}],
                "childReferences": [{"name": "tr-fail", "pipelineTaskName": "build-step"}],
            },
        }
        parts = []
        _build_pipelinerun_context(plr, "ns", parts)

        labels = [p[0] for p in parts]
        assert any("plr-1" in l for l in labels)
        assert any("build-step" in l for l in labels)

    @patch("calunga_release_watcher.analyzer._get_pod_logs", return_value={"step-build": "log output"})
    @patch("calunga_release_watcher.analyzer._get_child_taskruns")
    def test_includes_pod_logs(self, mock_child, mock_logs):
        tr = {
            "metadata": {"name": "tr-1", "namespace": "ns"},
            "status": {"conditions": [{"type": "Succeeded", "status": "False", "reason": "Failed", "message": ""}]},
        }
        mock_child.return_value = [tr]

        plr = {
            "metadata": {"name": "plr-1"},
            "status": {"conditions": [], "childReferences": []},
        }
        parts = []
        _build_pipelinerun_context(plr, "ns", parts)
        assert any("log output" in p[1] for p in parts)


# ---------------------------------------------------------------------------
# _build_context (mocked helpers)
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_no_namespace_returns_detail_only(self):
        body = {"kind": "PipelineRun", "metadata": {"namespace": ""}}
        info = SimpleNamespace(namespace="", log_prefix="[test]")
        result = _build_context(body, info, "some failure")
        assert "some failure" in result

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    def test_pipelinerun_path(self, mock_build_plr):
        body = {
            "kind": "PipelineRun",
            "metadata": {"name": "plr-1", "namespace": "ns"},
            "status": {"conditions": [{"status": "False", "reason": "Failed", "message": "err"}]},
        }
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        result = _build_context(body, info, "build failed")
        assert "build failed" in result
        mock_build_plr.assert_called_once()

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_test_pipelineruns_for_snapshot")
    def test_snapshot_path_with_failed_plrs(self, mock_get_plrs, mock_build_plr):
        failed_plr = {
            "metadata": {"name": "test-plr-1", "labels": {"test.appstudio.openshift.io/scenario": "scenario-a"}},
            "status": {"conditions": [{"type": "Succeeded", "status": "False"}]},
        }
        passed_plr = {
            "metadata": {"name": "test-plr-2", "labels": {}},
            "status": {"conditions": [{"type": "Succeeded", "status": "True"}]},
        }
        mock_get_plrs.return_value = [failed_plr, passed_plr]

        body = {"kind": "Snapshot", "metadata": {"name": "snap-1", "namespace": "ns"}, "status": {}}
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        result = _build_context(body, info, "tests failed")
        assert "1 failed" in result
        assert "1 passed" in result
        mock_build_plr.assert_called_once()

    @patch("calunga_release_watcher.analyzer._get_test_pipelineruns_for_snapshot", return_value=[])
    def test_snapshot_no_plrs(self, mock_get_plrs):
        body = {"kind": "Snapshot", "metadata": {"name": "snap-1", "namespace": "ns"}, "status": {}}
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        result = _build_context(body, info, "tests failed")
        assert "No test PipelineRuns found" in result


# ---------------------------------------------------------------------------
# Release failure context (managed PipelineRun)
# ---------------------------------------------------------------------------

RELEASE_BODY = {
    "kind": "Release",
    "metadata": {"name": "rel-1", "namespace": "lightwell-poc-tenant"},
    "status": {
        "conditions": [
            {"type": "Released", "status": "False", "reason": "Failed",
             "message": "Release processing failed on managed pipelineRun"},
            {"type": "ManagedPipelineProcessed", "status": "False", "reason": "Failed",
             "message": ""},
        ],
        "managedPipelineAttempts": [{
            "status": "Failed",
            "failureReason": "Error",
            "lastTask": "generate-security-metadata",
            "lastStep": "run",
            "pipelineRun": "rhtap-releng-tenant/managed-wgmdn",
            "successfulTasks": 6,
        }],
        "managedProcessing": {"pipelineRun": "rhtap-releng-tenant/managed-wgmdn"},
    },
}


class TestResolveManagedPipelinerunRef:
    def test_prefers_attempt_reference(self):
        status = {
            "managedPipelineAttempts": [{"pipelineRun": "ns/managed-attempt"}],
            "managedProcessing": {"pipelineRun": "ns/managed-processing"},
        }
        assert _resolve_managed_pipelinerun_ref(status) == "ns/managed-attempt"

    def test_latest_attempt_wins(self):
        status = {
            "managedPipelineAttempts": [
                {"pipelineRun": "ns/managed-old"},
                {"pipelineRun": "ns/managed-new"},
            ],
        }
        assert _resolve_managed_pipelinerun_ref(status) == "ns/managed-new"

    def test_falls_back_to_managed_processing(self):
        status = {"managedProcessing": {"pipelineRun": "ns/managed-processing"}}
        assert _resolve_managed_pipelinerun_ref(status) == "ns/managed-processing"

    def test_no_reference(self):
        assert _resolve_managed_pipelinerun_ref({}) == ""


class TestBuildReleaseFailureContext:
    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_managed_pipelinerun")
    def test_fetches_managed_pipelinerun_in_its_namespace(self, mock_get_plr, mock_build_plr):
        mock_get_plr.return_value = {"metadata": {"name": "managed-wgmdn"}}
        parts = []
        _build_release_failure_context(RELEASE_BODY, parts)

        mock_get_plr.assert_called_once_with("managed-wgmdn", "rhtap-releng-tenant")
        mock_build_plr.assert_called_once_with(
            {"metadata": {"name": "managed-wgmdn"}},
            "rhtap-releng-tenant",
            parts,
            plr_label="rhtap-releng-tenant/managed-wgmdn",
        )

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_managed_pipelinerun")
    def test_includes_conditions_and_attempts(self, mock_get_plr, mock_build_plr):
        mock_get_plr.return_value = {"metadata": {"name": "managed-wgmdn"}}
        parts = []
        _build_release_failure_context(RELEASE_BODY, parts)

        labels = [p[0] for p in parts]
        assert "Release condition 'Released'" in labels
        assert "Managed pipeline attempt" in labels
        attempt_content = next(p[1] for p in parts if p[0] == "Managed pipeline attempt")
        assert "generate-security-metadata" in attempt_content
        assert "rhtap-releng-tenant/managed-wgmdn" in attempt_content

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_managed_pipelinerun", return_value=None)
    def test_notes_gc_when_pipelinerun_gone(self, mock_get_plr, mock_build_plr):
        parts = []
        _build_release_failure_context(RELEASE_BODY, parts)
        content = _truncate_context(parts)
        assert "could not be fetched" in content
        assert "garbage-collected" in content
        mock_build_plr.assert_not_called()

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_managed_pipelinerun")
    def test_notes_missing_reference(self, mock_get_plr, mock_build_plr):
        body = {
            "kind": "Release",
            "metadata": {"name": "rel-1", "namespace": "ns"},
            "status": {"conditions": [
                {"type": "ManagedPipelineProcessed", "status": "False", "reason": "Failed",
                 "message": 'pipelineruns.tekton.dev "managed-1" is forbidden'},
            ]},
        }
        parts = []
        _build_release_failure_context(body, parts)
        content = _truncate_context(parts)
        assert "No managed PipelineRun reference" in content
        assert "is forbidden" in content
        mock_get_plr.assert_not_called()
        mock_build_plr.assert_not_called()

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    @patch("calunga_release_watcher.analyzer._get_managed_pipelinerun")
    def test_skips_conditions_without_messages(self, mock_get_plr, mock_build_plr):
        mock_get_plr.return_value = {"metadata": {"name": "managed-wgmdn"}}
        parts = []
        _build_release_failure_context(RELEASE_BODY, parts)
        labels = [p[0] for p in parts]
        assert "Release condition 'ManagedPipelineProcessed'" not in labels


class TestBuildContextReleasePath:
    @patch("calunga_release_watcher.analyzer._build_release_failure_context")
    def test_release_uses_release_path(self, mock_build_release):
        body = {
            "kind": "Release",
            "metadata": {"name": "rel-1", "namespace": "lightwell-poc-tenant"},
            "status": {},
        }
        info = SimpleNamespace(namespace="lightwell-poc-tenant", log_prefix="[test]")
        result = _build_context(body, info, "release failed")
        assert "release failed" in result
        mock_build_release.assert_called_once()

    @patch("calunga_release_watcher.analyzer._build_pipelinerun_context")
    def test_pipelinerun_path_unchanged(self, mock_build_plr):
        body = {
            "kind": "PipelineRun",
            "metadata": {"name": "plr-1", "namespace": "ns"},
            "status": {"conditions": [{"status": "False", "reason": "Failed", "message": "err"}]},
        }
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        _build_context(body, info, "build failed")
        mock_build_plr.assert_called_once()


# ---------------------------------------------------------------------------
# analyze_failure (integration of internal functions)
# ---------------------------------------------------------------------------


class TestAnalyzeFailure:
    @patch("calunga_release_watcher.analyzer.AI_ANALYSIS_ENABLED", False)
    def test_disabled(self):
        result = analyze_failure({}, None, PipelineState.BUILD_FAILED, "detail")
        assert result is None

    @patch("calunga_release_watcher.analyzer._call_claude")
    @patch("calunga_release_watcher.analyzer._build_context", return_value="some context")
    @patch("calunga_release_watcher.analyzer.AI_ANALYSIS_ENABLED", True)
    @patch("calunga_release_watcher.analyzer.GOOGLE_CLOUD_PROJECT", "proj")
    def test_calls_claude_and_attaches_scenarios(self, mock_build, mock_claude):
        mock_claude.return_value = FailureAnalysis(
            classification="fluke", confidence="high",
            root_cause="timeout", suggestion="retry",
            failed_task="build", failed_scenarios=[],
        )
        body = {
            "kind": "Snapshot",
            "metadata": {
                "annotations": {
                    "test.appstudio.openshift.io/status": json.dumps([
                        {"scenario": "s1", "status": "TestFailed"},
                    ]),
                },
            },
        }
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        result = analyze_failure(body, info, PipelineState.TESTS_FAILED, "detail")
        assert result is not None
        assert result.failed_scenarios == ["s1"]
        mock_claude.assert_called_once()

    @patch("calunga_release_watcher.analyzer._build_context", return_value="   ")
    @patch("calunga_release_watcher.analyzer.AI_ANALYSIS_ENABLED", True)
    @patch("calunga_release_watcher.analyzer.GOOGLE_CLOUD_PROJECT", "proj")
    def test_empty_context_returns_none(self, mock_build):
        info = SimpleNamespace(namespace="ns", log_prefix="[test]")
        result = analyze_failure({}, info, PipelineState.BUILD_FAILED, "detail")
        assert result is None
