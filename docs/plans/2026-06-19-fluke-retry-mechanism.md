# Retry Mechanism for Fluke Failures

## Context

The controller currently watches CI/CD pipeline resources and sends Slack notifications enriched with AI analysis (fluke/real/infra/unknown classification). When the LLM identifies a failure as a "fluke," no action is taken — the user must manually retry. This feature makes the controller act on fluke classifications by automatically retrying the failed step, transforming it from a read-only observer to an active participant.

## Max-Retries Tracking: In-Memory (V1)

The core design question was how to track retry counts when we don't always create the PipelineRun directly. **Solution: track retries in-memory on `PipelineInfo`.**

This works because:
- The controller already has pure in-memory state (no persistence)
- On pod restart, `set_live()` prunes terminal entries. Resources in `*_RETRYING` states survive and resume normally. Retry counts reset to 0, meaning at worst MAX_RETRIES additional attempts — acceptable for V1.
- Memory overhead is negligible: ~200-300 bytes per tracked SHA (two ints + a small dict of scenario->count). Only in-progress pipelines are tracked; terminal entries are pruned by `set_live()`.
- ConfigMap-based persistence can be added later if needed.

## Implementation Plan

### 1. Extract K8s client to shared module

Create [k8s.py](src/calunga_release_watcher/k8s.py) — move `_get_k8s_client()` from `analyzer.py` here as `get_k8s_client()`. Update `analyzer.py` imports. Both the analyzer and the new retrier will use this.

### 2. Config changes ([config.py](src/calunga_release_watcher/config.py))

New env-based config variables:
- `RETRY_ENABLED` (default `"false"`) — master gate for the feature
- `RETRY_CONFIDENCE_THRESHOLD` (default `"medium"`) — minimum AI confidence to trigger retry (`low`/`medium`/`high`)
- `RELEASE_PLAN` (default `"calunga"`) — needed when creating retry Release objects

New label/annotation constants:
- `LBL_SCENARIO = "test.appstudio.openshift.io/scenario"`
- `LBL_ITS_RUN = "test.appstudio.openshift.io/run"`
- `ANN_TEST_STATUS = "test.appstudio.openshift.io/status"` — Snapshot annotation containing a JSON array of test scenario results (scenario name, status like TestPassed/TestFailed, PLR name, timestamps)
- `LBL_RELEASE_PLAN`, `LBL_RELEASE_SNAPSHOT`, `LBL_AUTOMATED`

`MAX_RETRIES` already exists (default 3) and will be used.

### 3. State machine changes ([tracker.py](src/calunga_release_watcher/tracker.py))

**New states** in `PipelineState`:
- `BUILD_RETRYING`, `TESTS_RETRYING`, `RELEASE_RETRYING`

These are non-terminal states that allow transitions back to in-progress states when the retry's new resources appear (e.g., `TESTS_RETRYING` -> `TESTING` when a new test PLR arrives).

**New fields** on `PipelineInfo`:
- `build_retry_count: int = 0`
- `test_retry_counts: dict[str, int] = field(default_factory=dict)` — per-scenario
- `release_retry_count: int = 0`

**Stale-event guard**: When state is `*_RETRYING`, ignore Snapshot/Release events that would re-transition to the same `*_FAILED` state (the old failure condition may linger briefly after retry is triggered).

**Refactor `_fire_failure_notification`** into `_handle_failure`: same background thread pattern, but after sending the failure Slack notification, it calls the retrier. If retry succeeds, it directly sets state to `*_RETRYING` and sends a retry Slack notification.

### 4. Analyzer changes ([analyzer.py](src/calunga_release_watcher/analyzer.py))

**Switch from text parsing to structured output (Pydantic)**: Currently `_call_claude()` asks the LLM to respond in a text format and `_parse_response()` parses it with string splitting. This is fragile — the LLM might deviate from the format, and now that we rely on the classification/confidence for automated retry decisions, we need guaranteed structured output.

Replace the text-based approach with Anthropic's **structured outputs** feature:
- Convert `FailureAnalysis` from a `dataclass` to a Pydantic `BaseModel`
- Use `Literal["fluke", "real", "infra", "unknown"]` for `classification` and `Literal["high", "medium", "low"]` for `confidence` — the API constrains output to these exact values
- Call `client.messages.create()` with `output_config={"format": {"type": "json", "schema": FailureAnalysis.model_json_schema()}}` — forces JSON output matching the schema
- Parse response with `FailureAnalysis.model_validate_json(response.content[0].text)` — gives a typed Pydantic object
- Remove `_parse_response()` entirely — no manual parsing needed
- Update `SYSTEM_PROMPT` to describe the analysis task without specifying output format (the schema handles that)
- Add `pydantic` to project dependencies

Additional changes:
- Add `failed_scenarios: list[str]` field to `FailureAnalysis` dataclass
- Add `extract_failed_scenarios(body)` function — parses the Snapshot's `test.appstudio.openshift.io/status` JSON annotation to find scenarios with `TestFailed`/`EnvironmentProvisionError`/`DeploymentError` status
- Populate `failed_scenarios` in `analyze_failure()` when the body is a Snapshot

### 5. New module: [retrier.py](src/calunga_release_watcher/retrier.py)

Core functions:

**`should_retry(analysis, current_retry_count) -> bool`** — Decision gate: checks `RETRY_ENABLED`, classification is `"fluke"`, confidence meets threshold, count < `MAX_RETRIES`.

**`retry_test_scenarios(snapshot_name, namespace, failed_scenarios, info) -> list[str]`**
- Patches the Snapshot with label `test.appstudio.openshift.io/run=<value>` — this triggers the integration test service to re-create test PipelineRun(s)
- If single scenario failed: label value is the scenario name (e.g., `wheel-check-ubi9`)
- If multiple scenarios failed: label value is `all` (retries all failed scenarios at once)
- The integration controller removes the label after starting the scenario(s)
- Increments `info.test_retry_counts[scenario]` for each retried scenario

**`retry_release(original_release_body, snapshot_name, namespace, info) -> str | None`**
- Creates a new Release object with the same `spec.snapshot` and `spec.releasePlan`, copying relevant PAC labels/annotations from the original
- The release service automatically creates a new release PipelineRun
- Increments `info.release_retry_count`

**`retry_build(...)` — PLACEHOLDER**
- Logs a warning that build retry is not yet implemented
- Creating a build PipelineRun requires replicating PAC's behavior (git-auth secrets, complex pipelineSpec, task bundles) — needs separate design work

**`attempt_retry(analysis, failure_state, body, info) -> (bool, str)`** — Top-level orchestrator that dispatches to the appropriate retry mechanism based on failure state.

### 6. Deployment changes

- Update [configmap.yaml](deploy/configmap.yaml) with new env vars
- Document required RBAC changes (the K8S_TOKEN needs `patch` on snapshots and `create` on releases, in addition to existing read permissions)

### 7. Slack notifications (threaded)

The initial failure notification is sent as a **top-level message**. All retry-related follow-ups (retry attempt, subsequent failure/success) are sent as **thread replies** using the original message's `ts` (timestamp).

This requires:
- Updating `send_slack_sync()` in [slack.py](src/calunga_release_watcher/slack.py) to return the message `ts` from the Slack API response
- Adding a `send_slack_reply(message, thread_ts)` function that posts to the same channel with `thread_ts` parameter
- Storing the failure message `ts` on `PipelineInfo` (new field: `failure_thread_ts: str = ""`)
- Using the stored `ts` for retry notifications and any subsequent failure/success messages for the same pipeline

Retry message example (as thread reply):
```
🔄 Retrying test scenario 'wheel-check-ubi9' — attempt 1/3
```

Max retries exhausted (as thread reply):
```
⚠️ Max retries (3) exhausted — manual intervention required
```

Retry success (as thread reply):
```
✅ Retry succeeded — test scenario 'wheel-check-ubi9' passed
```

### 8. Safety

- **Feature gate**: `RETRY_ENABLED=false` by default — completely dormant until enabled
- **Hard retry cap**: `MAX_RETRIES` checked before every attempt
- **K8s API failures**: try/except around all writes; failure keeps the pipeline in `*_FAILED` state (no retry notification sent)
- **Duplicate prevention**: state machine prevents duplicate transitions; retries only trigger from `*_FAILED` transitions
- **Stale event guard**: `*_RETRYING` states ignore stale failure conditions from the previous cycle

### Files to modify/create

| File | Action |
|------|--------|
| `src/calunga_release_watcher/k8s.py` | **Create** — K8s client singleton |
| `src/calunga_release_watcher/retrier.py` | **Create** — retry logic |
| `src/calunga_release_watcher/config.py` | **Modify** — new config vars and constants |
| `src/calunga_release_watcher/analyzer.py` | **Modify** — `failed_scenarios` field, use shared k8s client |
| `src/calunga_release_watcher/tracker.py` | **Modify** — new states, retry fields, `_handle_failure` refactor |
| `src/calunga_release_watcher/slack.py` | **Modify** — return message `ts`, add `send_slack_reply()` for threaded messages |
| `deploy/configmap.yaml` | **Modify** — new env vars |

### Verification

1. Unit tests for `retrier.py`: `should_retry()` decision logic, K8s API mocking for `retry_test_scenarios()` and `retry_release()`
2. Unit tests for tracker state machine: `*_RETRYING` transitions, stale-event guards
3. Manual integration test: deploy with `RETRY_ENABLED=true`, `RETRY_CONFIDENCE_THRESHOLD=low`, observe a fluke failure being automatically retried with correct Slack messages
