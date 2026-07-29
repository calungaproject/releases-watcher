import logging

from kubernetes import client as k8s_client

from calunga_release_watcher.analyzer import FailureAnalysis
from calunga_release_watcher.config import (
    LBL_APPLICATION,
    LBL_COMPONENT,
    LBL_ITS_RUN,
    LBL_RELEASE_PLAN,
    LBL_RELEASE_SNAPSHOT,
    LBL_TEST_EVENT_TYPE,
    LBL_TEST_SHA,
    MAX_RETRIES,
    RELEASE_PLAN,
    RETRY_CONFIDENCE_THRESHOLD,
    RETRY_ENABLED,
    TENANT_NAMESPACE,
)
from calunga_release_watcher.k8s import get_k8s_client
from calunga_release_watcher.tracker import PipelineInfo, PipelineState

logger = logging.getLogger(__name__)

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def _meets_confidence_threshold(confidence: str) -> bool:
    return CONFIDENCE_ORDER.get(confidence, 0) >= CONFIDENCE_ORDER.get(
        RETRY_CONFIDENCE_THRESHOLD, 1
    )


def should_retry(analysis: FailureAnalysis, current_retry_count: int) -> bool:
    if not RETRY_ENABLED:
        return False
    if analysis.classification != "fluke":
        return False
    if not _meets_confidence_threshold(analysis.confidence):
        return False
    return current_retry_count < MAX_RETRIES


def retry_test_scenarios(
    snapshot_name: str,
    namespace: str,
    failed_scenarios: list[str],
    info: PipelineInfo,
) -> list[str]:
    if not failed_scenarios:
        logger.warning("%s No failed scenarios to retry", info.log_prefix)
        return []

    label_value = failed_scenarios[0] if len(failed_scenarios) == 1 else "all"

    api = k8s_client.CustomObjectsApi(get_k8s_client())
    try:
        api.patch_namespaced_custom_object(
            group="appstudio.redhat.com",
            version="v1alpha1",
            namespace=namespace,
            plural="snapshots",
            name=snapshot_name,
            body={
                "metadata": {
                    "labels": {LBL_ITS_RUN: label_value},
                },
            },
            _content_type="application/merge-patch+json",
        )
    except Exception:
        logger.exception(
            "%s Failed to patch Snapshot %s/%s for retry",
            info.log_prefix,
            namespace,
            snapshot_name,
        )
        return []

    for scenario in failed_scenarios:
        info.test_retry_counts[scenario] = info.test_retry_counts.get(scenario, 0) + 1

    logger.info(
        "%s Patched Snapshot %s with label %s=%s",
        info.log_prefix,
        snapshot_name,
        LBL_ITS_RUN,
        label_value,
    )
    return failed_scenarios


def retry_release(
    original_release_body: dict,
    snapshot_name: str,
    namespace: str,
    info: PipelineInfo,
) -> str | None:
    orig_labels = original_release_body.get("metadata", {}).get("labels", {})

    labels = {
        LBL_APPLICATION: orig_labels.get(LBL_APPLICATION, ""),
        LBL_COMPONENT: orig_labels.get(LBL_COMPONENT, ""),
        LBL_TEST_EVENT_TYPE: orig_labels.get(LBL_TEST_EVENT_TYPE, ""),
        LBL_TEST_SHA: orig_labels.get(LBL_TEST_SHA, ""),
        LBL_RELEASE_PLAN: RELEASE_PLAN,
        LBL_RELEASE_SNAPSHOT: snapshot_name,
    }
    labels = {k: v for k, v in labels.items() if v}

    release_obj = {
        "apiVersion": "appstudio.redhat.com/v1alpha1",
        "kind": "Release",
        "metadata": {
            "generateName": f"{snapshot_name}-retry-",
            "namespace": namespace,
            "labels": labels,
        },
        "spec": {
            "snapshot": snapshot_name,
            "releasePlan": RELEASE_PLAN,
        },
    }

    api = k8s_client.CustomObjectsApi(get_k8s_client())
    try:
        created = api.create_namespaced_custom_object(
            group="appstudio.redhat.com",
            version="v1alpha1",
            namespace=namespace,
            plural="releases",
            body=release_obj,
        )
    except Exception:
        logger.exception(
            "%s Failed to create retry Release in %s",
            info.log_prefix,
            namespace,
        )
        return None

    name = created.get("metadata", {}).get("name", "?")
    info.release_retry_count += 1
    logger.info("%s Created retry Release %s/%s", info.log_prefix, namespace, name)
    return name


def retry_build(info: PipelineInfo) -> None:
    logger.warning(
        "%s Build retry not implemented — requires replicating PAC behavior",
        info.log_prefix,
    )


def attempt_retry(
    analysis: FailureAnalysis,
    failure_state: PipelineState,
    body: dict,
    info: PipelineInfo,
) -> tuple[bool, str]:
    if failure_state == PipelineState.BUILD_FAILED:
        if not should_retry(analysis, info.build_retry_count):
            if info.build_retry_count >= MAX_RETRIES and RETRY_ENABLED:
                return False, f"⚠️ Max retries ({MAX_RETRIES}) exhausted for build — manual intervention required"
            return False, ""
        retry_build(info)
        return False, "⚠️ Build failure classified as fluke, but automatic build retry is not yet implemented — manual intervention required"

    if failure_state == PipelineState.TESTS_FAILED:
        max_count = max(
            (info.test_retry_counts.get(s, 0) for s in analysis.failed_scenarios),
            default=0,
        )
        if not should_retry(analysis, max_count):
            if max_count >= MAX_RETRIES and RETRY_ENABLED:
                return False, f"⚠️ Max retries ({MAX_RETRIES}) exhausted for tests — manual intervention required"
            return False, ""
        retried = retry_test_scenarios(
            info.snapshot, info.namespace, analysis.failed_scenarios, info,
        )
        if not retried:
            return False, ""
        scenarios_str = ", ".join(retried)
        return True, f"\U0001f504 Retrying test scenario(s): {scenarios_str} — attempt {max_count + 1}/{MAX_RETRIES}"

    if failure_state == PipelineState.RELEASE_FAILED:
        if not should_retry(analysis, info.release_retry_count):
            if info.release_retry_count >= MAX_RETRIES and RETRY_ENABLED:
                return False, f"⚠️ Max retries ({MAX_RETRIES}) exhausted for release — manual intervention required"
            return False, ""
        name = retry_release(body, info.snapshot, TENANT_NAMESPACE, info)
        if not name:
            return False, ""
        return True, f"\U0001f504 Retrying release — created {name} — attempt {info.release_retry_count}/{MAX_RETRIES}"

    return False, ""
