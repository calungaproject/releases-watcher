import enum
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from calunga_release_watcher.analyzer import analyze_failure, format_analysis
from calunga_release_watcher.config import (
    ANN_BUILD_SHA_TITLE,
    ANN_TEST_SHA,
    ANN_TEST_SHA_TITLE,
    LBL_BUILD_SHA,
    LBL_SNAPSHOT,
    LBL_TEST_SHA,
)
from calunga_release_watcher.slack import send_slack_sync

logger = logging.getLogger(__name__)


class PipelineState(enum.Enum):
    BUILD_RUNNING = "build_running"
    BUILD_SUCCEEDED = "build_succeeded"
    BUILD_FAILED = "build_failed"
    BUILD_RETRYING = "build_retrying"
    SNAPSHOT_CREATED = "snapshot_created"
    TESTING = "testing"
    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"
    TESTS_RETRYING = "tests_retrying"
    RELEASING = "releasing"
    RELEASED = "released"
    RELEASE_FAILED = "release_failed"
    RELEASE_RETRYING = "release_retrying"


FAILURE_STATES = {
    PipelineState.BUILD_FAILED,
    PipelineState.TESTS_FAILED,
    PipelineState.RELEASE_FAILED,
}

RETRYING_STATES = {
    PipelineState.BUILD_RETRYING,
    PipelineState.TESTS_RETRYING,
    PipelineState.RELEASE_RETRYING,
}

TERMINAL_STATES = FAILURE_STATES | {PipelineState.RELEASED}

# From a RETRYING state, block backward transitions to earlier pipeline stages.
# RETRYING → FAILURE and RETRYING → RELEASED are always allowed.
_RETRYING_BLOCKED_TRANSITIONS: dict[PipelineState, frozenset] = {
    PipelineState.RELEASE_RETRYING: frozenset({
        PipelineState.BUILD_RUNNING,
        PipelineState.BUILD_SUCCEEDED,
        PipelineState.SNAPSHOT_CREATED,
        PipelineState.TESTING,
        PipelineState.TESTS_PASSED,
        PipelineState.RELEASING,
    }),
    PipelineState.TESTS_RETRYING: frozenset({
        PipelineState.BUILD_RUNNING,
        PipelineState.BUILD_SUCCEEDED,
        PipelineState.SNAPSHOT_CREATED,
    }),
}


@dataclass
class PipelineInfo:
    sha: str
    sha_short: str
    package_title: str
    namespace: str = ""
    state: PipelineState = PipelineState.BUILD_RUNNING
    build_pipelinerun: str = ""
    snapshot: str = ""
    test_pipelineruns: dict[str, str] = field(default_factory=dict)
    expected_tests: int = 0
    release: str = ""
    release_pipelinerun: str = ""
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Retry tracking
    build_retry_count: int = 0
    test_retry_counts: dict[str, int] = field(default_factory=dict)
    release_retry_count: int = 0
    failure_thread_ts: str = ""
    processed_failure_sources: set[str] = field(default_factory=set)

    @property
    def log_prefix(self) -> str:
        return f"[{self.package_title} sha={self.sha_short}]"


def extract_sha(body: dict) -> str | None:
    labels = body.get("metadata", {}).get("labels", {})
    annotations = body.get("metadata", {}).get("annotations", {})
    return labels.get(LBL_TEST_SHA) or labels.get(LBL_BUILD_SHA) or annotations.get(ANN_TEST_SHA)


def extract_package_title(body: dict) -> str:
    labels = body.get("metadata", {}).get("labels", {})
    annotations = body.get("metadata", {}).get("annotations", {})
    title = (
        labels.get(ANN_TEST_SHA_TITLE)
        or annotations.get(ANN_TEST_SHA_TITLE)
        or annotations.get(ANN_BUILD_SHA_TITLE, "")
    )
    title = title.split("\n")[0]
    if title.startswith("Automatic build "):
        title = title[len("Automatic build "):]
    return title or "unknown"


def extract_snapshot_name(body: dict) -> str:
    labels = body.get("metadata", {}).get("labels", {})
    return labels.get(LBL_SNAPSHOT, "")


def get_condition_status(body: dict) -> tuple[str | None, str | None]:
    conditions = body.get("status", {}).get("conditions", [])
    if not conditions:
        return None, None
    cond = conditions[0]
    return cond.get("status"), cond.get("reason")


def get_named_condition(body: dict, condition_type: str) -> tuple[str | None, str | None]:
    conditions = body.get("status", {}).get("conditions", [])
    for cond in conditions:
        if cond.get("type") == condition_type:
            return cond.get("status"), cond.get("reason")
    return None, None


def _fire_slack(message: str, thread_ts: str = "") -> None:
    threading.Thread(target=send_slack_sync, args=(message, thread_ts), daemon=True).start()


def _handle_failure(
    body: dict | None, info: "PipelineInfo", state: "PipelineState", detail: str,
) -> None:
    from calunga_release_watcher.retrier import attempt_retry

    def _worker():
        analysis = None
        if body is not None:
            try:
                analysis = analyze_failure(
                    body=body, info=info, failure_state=state, detail=detail,
                )
            except Exception:
                logger.exception("%s AI analysis failed", info.log_prefix)

        slack_msg = f"❌ {info.package_title} (sha={info.sha_short}) — {detail}"
        if analysis:
            slack_msg += format_analysis(analysis)
        ts = send_slack_sync(slack_msg)
        if ts:
            info.failure_thread_ts = ts

        if analysis is None or body is None:
            return

        retried, retry_msg = attempt_retry(analysis, state, body, info)
        if retried:
            info.state = {
                PipelineState.BUILD_FAILED: PipelineState.BUILD_RETRYING,
                PipelineState.TESTS_FAILED: PipelineState.TESTS_RETRYING,
                PipelineState.RELEASE_FAILED: PipelineState.RELEASE_RETRYING,
            }.get(state, state)
            info.last_updated = datetime.now(timezone.utc)
        if retry_msg:
            logger.info("%s %s", info.log_prefix, retry_msg)
            if info.failure_thread_ts:
                send_slack_sync(retry_msg, info.failure_thread_ts)

    threading.Thread(target=_worker, daemon=True).start()


class PipelineTracker:
    def __init__(self) -> None:
        self._pipelines: dict[str, PipelineInfo] = {}
        self._seen_shas: set[str] = set()
        self._live = False

    def set_live(self) -> None:
        total = len(self._pipelines)
        finished = sum(1 for p in self._pipelines.values() if p.state in TERMINAL_STATES)
        stale = sum(
            1 for p in self._pipelines.values()
            if p.state not in TERMINAL_STATES and not p.build_pipelinerun
        )
        self._pipelines = {
            sha: p for sha, p in self._pipelines.items()
            if p.state not in TERMINAL_STATES and p.build_pipelinerun
        }
        watching = len(self._pipelines)
        logger.info(
            "Initial sync complete — %d resources seen, %d finished, %d stale (no build PLR), watching %d in-progress",
            total,
            finished,
            stale,
            watching,
        )
        for p in self._pipelines.values():
            logger.info(
                "%s in progress — state: %s, build: %s",
                p.log_prefix,
                p.state.value,
                p.build_pipelinerun,
            )
        self._live = True

    def get_or_create(self, sha: str, body: dict) -> PipelineInfo | None:
        if sha in self._pipelines:
            return self._pipelines[sha]
        if self._live and sha in self._seen_shas:
            return None
        self._seen_shas.add(sha)
        self._pipelines[sha] = PipelineInfo(
            sha=sha,
            sha_short=sha[:7],
            package_title=extract_package_title(body),
        )
        return self._pipelines[sha]

    def get(self, sha: str) -> PipelineInfo | None:
        return self._pipelines.get(sha)

    def _transition(
        self, info: PipelineInfo, new_state: PipelineState,
        detail: str = "", body: dict | None = None,
    ) -> None:
        old_state = info.state
        if new_state == old_state:
            return
        if old_state in TERMINAL_STATES:
            return
        if new_state in _RETRYING_BLOCKED_TRANSITIONS.get(old_state, frozenset()):
            return
        info.state = new_state
        info.last_updated = datetime.now(timezone.utc)

        if not self._live:
            return

        msg = f"{info.log_prefix} {new_state.value}"
        if detail:
            msg += f" — {detail}"

        if new_state in FAILURE_STATES:
            logger.error(msg)
            _handle_failure(body, info, new_state, detail)
        elif new_state == PipelineState.RELEASED:
            logger.info(msg)
            release_msg = (
                f"✅ {info.package_title} (sha={info.sha_short}) — pipeline complete. "
                f"Released via {info.release_pipelinerun}."
            )
            if info.failure_thread_ts:
                send_slack_sync(release_msg, info.failure_thread_ts)
            else:
                _fire_slack(release_msg)
        else:
            logger.info(msg)

    def on_build_pipelinerun(self, body: dict) -> None:
        sha = extract_sha(body)
        if not sha:
            return
        name = body["metadata"]["name"]
        status, reason = get_condition_status(body)
        info = self.get_or_create(sha, body)
        if info is None:
            return
        info.build_pipelinerun = name
        info.namespace = body["metadata"]["namespace"]

        if status is None:
            self._transition(info, PipelineState.BUILD_RUNNING, f"Build PipelineRun started: {name}")
        elif status == "True":
            self._transition(info, PipelineState.BUILD_SUCCEEDED, f"Build PipelineRun succeeded: {name}")
        elif status == "False":
            source_key = f"build:{name}"
            if source_key not in info.processed_failure_sources:
                info.processed_failure_sources.add(source_key)
                self._transition(
                    info,
                    PipelineState.BUILD_FAILED,
                    f"Build PipelineRun FAILED: {name} (reason={reason})",
                    body=body,
                )

    def on_snapshot(self, body: dict) -> None:
        sha = extract_sha(body)
        if not sha:
            return
        name = body["metadata"]["name"]
        info = self.get_or_create(sha, body)
        if info is None:
            return
        info.snapshot = name
        info.namespace = body["metadata"]["namespace"]

        test_status, _ = get_named_condition(body, "AppStudioTestSucceeded")
        release_status, _ = get_named_condition(body, "AutoReleased")

        if test_status == "False":
            source_key = f"snapshot:{name}"
            if source_key not in info.processed_failure_sources:
                info.processed_failure_sources.add(source_key)
                self._transition(info, PipelineState.TESTS_FAILED, f"Tests failed (via Snapshot {name})", body=body)
        elif test_status == "True" and release_status == "True":
            pass
        elif test_status == "True":
            self._transition(info, PipelineState.TESTS_PASSED, f"All tests passed (via Snapshot {name})")
        else:
            self._transition(info, PipelineState.SNAPSHOT_CREATED, f"Snapshot created: {name}")

    def on_test_pipelinerun(self, body: dict) -> None:
        sha = extract_sha(body)
        if not sha:
            return
        name = body["metadata"]["name"]
        labels = body.get("metadata", {}).get("labels", {})
        scenario = labels.get("test.appstudio.openshift.io/scenario", name)
        status, reason = get_condition_status(body)

        info = self.get_or_create(sha, body)
        if info is None:
            return
        prev_status = info.test_pipelineruns.get(name)
        info.test_pipelineruns[name] = status or "Unknown"
        info.namespace = body["metadata"]["namespace"]

        if info.state in (PipelineState.SNAPSHOT_CREATED, PipelineState.BUILD_SUCCEEDED):
            self._transition(info, PipelineState.TESTING, f"Test started: {scenario}")

        if status == "True" and self._live and prev_status != "True":
            passed = sum(1 for s in info.test_pipelineruns.values() if s == "True")
            total = len(info.test_pipelineruns)
            logger.info(
                "%s Test passed: %s (%d/%d)",
                info.log_prefix,
                scenario,
                passed,
                total,
            )
        elif status == "False" and self._live and prev_status != "False":
            logger.warning(
                "%s Test FAILED: %s (reason=%s) — waiting for Snapshot condition",
                info.log_prefix, scenario, reason,
            )

    def on_release(self, body: dict) -> None:
        sha = extract_sha(body)
        if not sha:
            return
        name = body["metadata"]["name"]
        info = self.get_or_create(sha, body)
        if info is None:
            return
        info.release = name
        info.namespace = body["metadata"]["namespace"]

        released_status, released_reason = get_named_condition(body, "Released")
        managed_status, _ = get_named_condition(body, "ManagedPipelineProcessed")

        managed_processing = body.get("status", {}).get("managedProcessing", {})
        plr_ref = managed_processing.get("pipelineRun", "")
        if plr_ref:
            info.release_pipelinerun = plr_ref

        if released_status == "True":
            self._transition(info, PipelineState.RELEASED, f"Release succeeded: {name}")
        elif released_status == "False" and released_reason not in ("Progressing", "Running"):
            source_key = f"release:{name}"
            if source_key not in info.processed_failure_sources:
                info.processed_failure_sources.add(source_key)
                self._transition(
                    info,
                    PipelineState.RELEASE_FAILED,
                    f"Release FAILED: {name} (reason={released_reason})",
                    body=body,
                )
        else:
            self._transition(info, PipelineState.RELEASING, f"Release created: {name}")

    def on_release_pipelinerun(self, body: dict) -> None:
        sha = extract_sha(body)
        if not sha:
            return
        name = body["metadata"]["name"]
        namespace = body["metadata"]["namespace"]
        status, reason = get_condition_status(body)

        info = self.get_or_create(sha, body)
        if info is None:
            return
        already_tracked = info.release_pipelinerun == f"{namespace}/{name}"
        info.release_pipelinerun = f"{namespace}/{name}"
        info.namespace = namespace

        if status is None and self._live and not already_tracked:
            logger.info(
                "%s Release PipelineRun started: %s (%s)",
                info.log_prefix,
                name,
                namespace,
            )
        elif status == "True":
            self._transition(
                info,
                PipelineState.RELEASED,
                f"Release PipelineRun succeeded: {name} — PIPELINE COMPLETE",
            )
        elif status == "False":
            source_key = f"plr:{namespace}/{name}"
            if source_key not in info.processed_failure_sources:
                info.processed_failure_sources.add(source_key)
                self._transition(
                    info,
                    PipelineState.RELEASE_FAILED,
                    f"Release PipelineRun FAILED: {name} (reason={reason})",
                    body=body,
                )
