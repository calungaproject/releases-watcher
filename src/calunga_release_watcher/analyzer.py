import json
import logging
from typing import Literal

from pydantic import BaseModel
from kubernetes import client as k8s_client

from anthropic import AnthropicVertex

from calunga_release_watcher.config import (
    AI_MAX_LOG_LINES,
    AI_MODEL,
    AI_TIMEOUT_SECONDS,
    AI_ANALYSIS_ENABLED,
    ANN_TEST_STATUS,
    GOOGLE_CLOUD_PROJECT,
    GOOGLE_CLOUD_REGION,
    LBL_PIPELINE_TYPE,
    LBL_SNAPSHOT,
)
from calunga_release_watcher.k8s import get_k8s_client

logger = logging.getLogger(__name__)

MAX_TOTAL_LOG_CHARS = 50_000

SYSTEM_PROMPT = """\
You are an expert CI/CD failure analyst for Tekton pipelines running in Konflux/RHTAP.

Analyze pipeline failures and classify them as:
- "fluke": Transient/intermittent failure that would likely pass on retry \
(network timeouts, image pull failures from registries like quay.io or registry.redhat.io, \
rate limits, DNS resolution failures, temporary infrastructure issues, \
git push rejections due to non-fast-forward / concurrent pushes to a shared repository, \
HTTP 400 errors when uploading packages to Pulp/PyPI repositories like packages.redhat.com)
- "real": Genuine code or configuration problem that requires human attention \
(compilation errors, test assertion failures, missing dependencies, security policy violations, \
broken build scripts)
- "infra": Infrastructure problem outside the developer's control \
(cluster issues, storage problems, node failures, certificate expiration, OOM kills)
- "unknown": Insufficient information to determine the cause

Provide your analysis as structured JSON.\
"""

USER_PROMPT_TEMPLATE = """\
A Tekton pipeline has failed with state: {failure_state}

Here is the failure context from the Kubernetes cluster:

{context}

Analyze this failure. What went wrong, and is this a fluke, a real issue, \
or an infrastructure problem?\
"""


class FailureAnalysis(BaseModel):
    classification: Literal["fluke", "real", "infra", "unknown"]
    confidence: Literal["high", "medium", "low"]
    root_cause: str
    suggestion: str
    failed_task: str
    failed_scenarios: list[str] = []


# ---------------------------------------------------------------------------
# K8s context gathering
# ---------------------------------------------------------------------------


def _get_child_taskruns(body: dict, namespace: str) -> list[dict]:
    child_refs = body.get("status", {}).get("childReferences", [])
    if not child_refs:
        return []

    api = k8s_client.CustomObjectsApi(get_k8s_client())
    taskruns = []
    for ref in child_refs:
        if ref.get("kind") != "TaskRun":
            continue
        try:
            tr = api.get_namespaced_custom_object(
                group="tekton.dev",
                version="v1",
                namespace=namespace,
                plural="taskruns",
                name=ref["name"],
            )
            taskruns.append(tr)
        except Exception:
            logger.warning("Failed to fetch TaskRun %s/%s", namespace, ref["name"])
    return taskruns


def _get_test_pipelineruns_for_snapshot(snapshot_name: str, namespace: str) -> list[dict]:
    api = k8s_client.CustomObjectsApi(get_k8s_client())
    try:
        result = api.list_namespaced_custom_object(
            group="tekton.dev",
            version="v1",
            namespace=namespace,
            plural="pipelineruns",
            label_selector=f"{LBL_SNAPSHOT}={snapshot_name},{LBL_PIPELINE_TYPE}=test",
        )
        return result.get("items", [])
    except Exception:
        logger.warning("Failed to list test PipelineRuns for snapshot %s/%s", namespace, snapshot_name)
        return []


def _get_failed_taskruns(taskruns: list[dict]) -> list[dict]:
    failed = []
    for tr in taskruns:
        conditions = tr.get("status", {}).get("conditions", [])
        for c in conditions:
            if c.get("type") == "Succeeded" and c.get("status") == "False":
                failed.append(tr)
                break
    return failed


def _get_pod_logs(taskrun: dict, namespace: str, max_lines: int) -> dict[str, str]:
    pod_name = taskrun.get("status", {}).get("podName", "")
    if not pod_name:
        return {}

    core_v1 = k8s_client.CoreV1Api(get_k8s_client())
    logs: dict[str, str] = {}

    try:
        pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        containers = [c.name for c in (pod.spec.containers or [])]
        containers += [c.name for c in (pod.spec.init_containers or [])]
    except Exception:
        logger.warning("Failed to read pod %s/%s", namespace, pod_name)
        return {}

    for container_name in containers:
        try:
            log = core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container_name,
                tail_lines=max_lines,
            )
            if log:
                logs[container_name] = log
        except Exception:
            pass

    return logs


# ---------------------------------------------------------------------------
# Context assembly and truncation
# ---------------------------------------------------------------------------


def _truncate_context(context_parts: list[tuple[str, str]]) -> str:
    result = []
    budget = MAX_TOTAL_LOG_CHARS

    for label, content in context_parts:
        if budget <= 0:
            result.append(f"\n--- {label}: [TRUNCATED] ---\n")
            continue
        if len(content) > budget:
            content = content[-budget:]
            content = f"[... truncated, showing last {budget} chars ...]\n" + content
        result.append(f"\n--- {label} ---\n{content}")
        budget -= len(content)

    return "\n".join(result)


def _build_pipelinerun_context(
    plr: dict, namespace: str, context_parts: list[tuple[str, str]], plr_label: str = "",
) -> None:
    plr_name = plr.get("metadata", {}).get("name", "?")
    label = plr_label or plr_name

    conditions = plr.get("status", {}).get("conditions", [])
    if conditions:
        c = conditions[0]
        context_parts.append((
            f"PipelineRun '{label}' Condition",
            f"status={c.get('status')} reason={c.get('reason')} "
            f"message={c.get('message', '')}",
        ))

    taskruns = _get_child_taskruns(plr, namespace)
    failed_taskruns = _get_failed_taskruns(taskruns)

    target_taskruns = failed_taskruns if failed_taskruns else taskruns[-3:]
    for tr in target_taskruns:
        tr_name = tr.get("metadata", {}).get("name", "?")
        pipeline_task = ""
        for ref in plr.get("status", {}).get("childReferences", []):
            if ref.get("name") == tr.get("metadata", {}).get("name"):
                pipeline_task = ref.get("pipelineTaskName", "")
                break

        tr_label = pipeline_task or tr_name

        tr_conditions = tr.get("status", {}).get("conditions", [])
        if tr_conditions:
            c = tr_conditions[0]
            context_parts.append((
                f"TaskRun '{tr_label}' Condition",
                f"status={c.get('status')} reason={c.get('reason')} "
                f"message={c.get('message', '')}",
            ))

        tr_ns = tr.get("metadata", {}).get("namespace", namespace)
        pod_logs = _get_pod_logs(tr, tr_ns, AI_MAX_LOG_LINES)
        for container, log in pod_logs.items():
            context_parts.append((
                f"TaskRun '{tr_label}' / Container '{container}' "
                f"(last {AI_MAX_LOG_LINES} lines)",
                log,
            ))


def _build_context(body: dict, info, detail: str) -> str:
    namespace = info.namespace or body.get("metadata", {}).get("namespace", "")
    context_parts: list[tuple[str, str]] = []

    context_parts.append(("Failure Detail", detail))

    if not namespace:
        return _truncate_context(context_parts)

    is_snapshot = body.get("kind") == "Snapshot"

    if is_snapshot:
        snapshot_name = body.get("metadata", {}).get("name", "")
        test_plrs = _get_test_pipelineruns_for_snapshot(snapshot_name, namespace)
        if not test_plrs:
            context_parts.append(("Note", f"No test PipelineRuns found for snapshot {snapshot_name} (may be pruned)"))
            return _truncate_context(context_parts)

        failed_plrs = [
            plr for plr in test_plrs
            if any(
                c.get("type") == "Succeeded" and c.get("status") == "False"
                for c in plr.get("status", {}).get("conditions", [])
            )
        ]
        passed_plrs = [
            plr for plr in test_plrs
            if any(
                c.get("type") == "Succeeded" and c.get("status") == "True"
                for c in plr.get("status", {}).get("conditions", [])
            )
        ]

        context_parts.append((
            "Test Summary",
            f"{len(test_plrs)} test PipelineRuns total, "
            f"{len(failed_plrs)} failed, {len(passed_plrs)} passed",
        ))

        for plr in failed_plrs:
            labels = plr.get("metadata", {}).get("labels", {})
            scenario = labels.get("test.appstudio.openshift.io/scenario", plr["metadata"]["name"])
            _build_pipelinerun_context(plr, namespace, context_parts, plr_label=scenario)

    else:
        conditions = body.get("status", {}).get("conditions", [])
        if conditions:
            cond = conditions[0]
            context_parts.append((
                "PipelineRun Condition",
                f"status={cond.get('status')} reason={cond.get('reason')} "
                f"message={cond.get('message', '')}",
            ))
        _build_pipelinerun_context(body, namespace, context_parts)

    return _truncate_context(context_parts)


# ---------------------------------------------------------------------------
# Failed scenario extraction (from Snapshot annotation)
# ---------------------------------------------------------------------------

FAILED_TEST_STATUSES = {"TestFail", "TestFailed", "EnvironmentProvisionError", "DeploymentError"}


def extract_failed_scenarios(body: dict) -> list[str]:
    if body.get("kind") != "Snapshot":
        return []
    annotations = body.get("metadata", {}).get("annotations", {})
    raw = annotations.get(ANN_TEST_STATUS, "")
    if not raw:
        return []
    try:
        statuses = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse test status annotation")
        return []
    return [
        s["scenario"]
        for s in statuses
        if isinstance(s, dict) and s.get("status") in FAILED_TEST_STATUSES and s.get("scenario")
    ]


# ---------------------------------------------------------------------------
# Claude invocation and structured output
# ---------------------------------------------------------------------------


def _call_claude(context: str, failure_state: str) -> FailureAnalysis | None:
    if not GOOGLE_CLOUD_PROJECT:
        logger.warning("GOOGLE_CLOUD_PROJECT not set — skipping AI analysis")
        return None

    client = AnthropicVertex(
        project_id=GOOGLE_CLOUD_PROJECT,
        region=GOOGLE_CLOUD_REGION,
    )

    response = client.messages.create(
        model=AI_MODEL,
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    failure_state=failure_state,
                    context=context,
                ),
            },
        ],
        timeout=AI_TIMEOUT_SECONDS,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    **FailureAnalysis.model_json_schema(),
                    "additionalProperties": False,
                },
            },
        },
    )

    return FailureAnalysis.model_validate_json(response.content[0].text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_failure(body: dict, info, failure_state, detail: str) -> FailureAnalysis | None:
    if not AI_ANALYSIS_ENABLED or not GOOGLE_CLOUD_PROJECT:
        return None

    context = _build_context(body, info, detail)
    if not context.strip():
        logger.info("No context gathered for AI analysis")
        return None

    analysis = _call_claude(context, failure_state.value)
    if analysis is not None:
        analysis.failed_scenarios = extract_failed_scenarios(body)
    return analysis


def format_analysis(analysis: FailureAnalysis) -> str:
    emoji = {
        "fluke": "\U0001f504",   # 🔄
        "real": "\U0001f534",    # 🔴
        "infra": "\U0001f3d7️",  # 🏗️
        "unknown": "❓",     # ❓
    }.get(analysis.classification, "❓")

    confidence_emoji = {
        "high": "\U0001f7e2",    # 🟢
        "medium": "\U0001f7e1",  # 🟡
        "low": "\U0001f534",     # 🔴
    }.get(analysis.confidence, "\U0001f534")

    parts = [
        f"\n\n{emoji} *AI Analysis* ({confidence_emoji} {analysis.confidence} confidence)",
        f"*Classification:* {analysis.classification}",
        f"*Failed Task:* {analysis.failed_task}",
        f"*Root Cause:* {analysis.root_cause}",
    ]
    if analysis.suggestion and analysis.suggestion.lower() != "none":
        parts.append(f"*Suggestion:* {analysis.suggestion}")

    return "\n".join(parts)
