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
    LBL_BUILD_PLR,
    LBL_BUILD_SHA,
    LBL_COMPONENT,
    LBL_RELEASE_SNAPSHOT,
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

# A failed release can be retried outside the watcher by creating a new Release
# or managed PipelineRun for the same tracked build.  Keep other terminal states
# immutable, but allow handlers to reopen this one when they observe a distinct
# release attempt.
_TERMINAL_RECOVERY_TRANSITIONS = {
    (PipelineState.RELEASE_FAILED, PipelineState.RELEASE_RETRYING),
}

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
    release_attempt_sources: set[str] = field(default_factory=set)
    git_url: str = ""
    git_commit: str = ""

    @property
    def git_commit_short(self) -> str:
        return self.git_commit[:7] if self.git_commit else ""

    @property
    def log_prefix(self) -> str:
        if self.git_commit:
            return f"[{self.package_title} commit={self.git_commit_short}]"
        return f"[{self.package_title} key={self.sha_short}]"

    @property
    def git_summary(self) -> str:
        if self.git_url and self.git_commit:
            return f"{self.git_url} @ {self.git_commit_short}"
        if self.git_commit:
            return self.git_commit_short
        return self.sha_short


def extract_tracking_key(body: dict) -> str | None:
    labels = body.get("metadata", {}).get("labels", {})
    annotations = body.get("metadata", {}).get("annotations", {})
    sha = labels.get(LBL_TEST_SHA) or labels.get(LBL_BUILD_SHA) or annotations.get(ANN_TEST_SHA)
    if sha:
        return sha
    return labels.get(LBL_BUILD_PLR)


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
    if title:
        return title
    for p in body.get("spec", {}).get("params", []):
        if p.get("name") == "IMAGE":
            image = p.get("value", "")
            if ":" in image:
                return image.rsplit(":", 1)[-1].split("@")[0]
    return labels.get(LBL_COMPONENT) or body.get("metadata", {}).get("name", "unknown")


def extract_git_info(body: dict) -> tuple[str, str]:
    url = ""
    commit = ""
    for r in body.get("status", {}).get("results", []):
        if r.get("name") == "CHAINS-GIT_URL":
            url = r.get("value", "")
        elif r.get("name") == "CHAINS-GIT_COMMIT":
            commit = r.get("value", "")
    if url and commit:
        return url, commit
    for comp in body.get("spec", {}).get("components", []):
        git = comp.get("source", {}).get("git", {})
        if git.get("url") and git.get("revision"):
            return git["url"], git["revision"]
    return url, commit


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

        slack_msg = f"❌ {info.package_title} ({info.git_summary}) — {detail}"
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
        self._snapshot_index: dict[str, str] = {}
        self._seen_keys: set[str] = set()
        self._live = False

    def set_live(self) -> None:
        total = len(self._pipelines)
        finished = sum(1 for p in self._pipelines.values() if p.state in TERMINAL_STATES)
        stale = sum(
            1 for p in self._pipelines.values()
            if p.state not in TERMINAL_STATES and not p.build_pipelinerun
        )
        self._pipelines = {
            key: p for key, p in self._pipelines.items()
            if p.state not in TERMINAL_STATES and p.build_pipelinerun
        }
        active_keys = set(self._pipelines.keys())
        self._snapshot_index = {
            snap: key for snap, key in self._snapshot_index.items()
            if key in active_keys
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

    def get_or_create(self, key: str, body: dict) -> PipelineInfo | None:
        if key in self._pipelines:
            return self._pipelines[key]
        if self._live and key in self._seen_keys:
            return None
        self._seen_keys.add(key)
        self._pipelines[key] = PipelineInfo(
            sha=key,
            sha_short=key[:12],
            package_title=extract_package_title(body),
        )
        return self._pipelines[key]

    def get(self, key: str) -> PipelineInfo | None:
        return self._pipelines.get(key)

    def _transition(
        self, info: PipelineInfo, new_state: PipelineState,
        detail: str = "", body: dict | None = None,
    ) -> None:
        old_state = info.state
        if new_state == old_state:
            return
        if (
            old_state in TERMINAL_STATES
            and (old_state, new_state) not in _TERMINAL_RECOVERY_TRANSITIONS
        ):
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
                f"✅ {info.package_title} ({info.git_summary}) — pipeline complete. "
                f"Released via {info.release_pipelinerun}."
            )
            if info.failure_thread_ts:
                send_slack_sync(release_msg, info.failure_thread_ts)
            else:
                _fire_slack(release_msg)
        else:
            logger.info(msg)

    def on_build_pipelinerun(self, body: dict) -> None:
        name = body["metadata"]["name"]
        key = extract_tracking_key(body) or name
        status, reason = get_condition_status(body)
        info = self.get_or_create(key, body)
        if info is None:
            return
        info.build_pipelinerun = name
        info.namespace = body["metadata"]["namespace"]

        if status is None:
            self._transition(info, PipelineState.BUILD_RUNNING, f"Build PipelineRun started: {name}")
        elif status == "True":
            url, commit = extract_git_info(body)
            if url:
                info.git_url = url
            if commit:
                info.git_commit = commit
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
        labels = body.get("metadata", {}).get("labels", {})
        key = extract_tracking_key(body) or labels.get(LBL_BUILD_PLR)
        if not key:
            return
        name = body["metadata"]["name"]
        info = self.get_or_create(key, body)
        if info is None:
            return
        info.snapshot = name
        info.namespace = body["metadata"]["namespace"]
        self._snapshot_index[name] = key
        if not info.git_url or not info.git_commit:
            url, commit = extract_git_info(body)
            if url:
                info.git_url = url
            if commit:
                info.git_commit = commit

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
        key = extract_tracking_key(body)
        if not key:
            return
        name = body["metadata"]["name"]
        labels = body.get("metadata", {}).get("labels", {})
        scenario = labels.get("test.appstudio.openshift.io/scenario", name)
        status, reason = get_condition_status(body)

        info = self.get_or_create(key, body)
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
        labels = body.get("metadata", {}).get("labels", {})
        key = extract_tracking_key(body)
        if not key:
            snapshot_name = labels.get(LBL_RELEASE_SNAPSHOT, "")
            key = self._snapshot_index.get(snapshot_name)
        if not key:
            return
        name = body["metadata"]["name"]
        info = self.get_or_create(key, body)
        if info is None:
            return
        release_source = f"release:{body['metadata']['namespace']}/{name}"
        managed_processing = body.get("status", {}).get("managedProcessing", {})
        plr_ref = managed_processing.get("pipelineRun", "")
        plr_source = f"plr:{plr_ref}" if plr_ref else ""
        is_new_release_attempt = (
            release_source not in info.release_attempt_sources
            and (
                not plr_source
                or plr_source not in info.release_attempt_sources
            )
        )
        info.release_attempt_sources.add(release_source)
        if plr_source:
            info.release_attempt_sources.add(plr_source)
        info.release = name
        info.namespace = body["metadata"]["namespace"]

        if is_new_release_attempt and info.state == PipelineState.RELEASE_FAILED:
            self._transition(
                info,
                PipelineState.RELEASE_RETRYING,
                f"New release attempt detected: {name}",
            )

        released_status, released_reason = get_named_condition(body, "Released")
        managed_status, _ = get_named_condition(body, "ManagedPipelineProcessed")

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
        labels = body.get("metadata", {}).get("labels", {})
        key = extract_tracking_key(body)
        if not key:
            snapshot_name = labels.get(LBL_SNAPSHOT, "")
            key = self._snapshot_index.get(snapshot_name)
        if not key:
            return
        name = body["metadata"]["name"]
        namespace = body["metadata"]["namespace"]
        status, reason = get_condition_status(body)

        info = self.get_or_create(key, body)
        if info is None:
            return
        plr_ref = f"{namespace}/{name}"
        attempt_source = f"plr:{plr_ref}"
        is_new_release_attempt = (
            attempt_source not in info.release_attempt_sources
        )
        info.release_attempt_sources.add(attempt_source)
        info.release_pipelinerun = plr_ref
        info.namespace = namespace

        if is_new_release_attempt and info.state == PipelineState.RELEASE_FAILED:
            self._transition(
                info,
                PipelineState.RELEASE_RETRYING,
                f"New release PipelineRun attempt detected: {name}",
            )

        if status is None and self._live and is_new_release_attempt:
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
