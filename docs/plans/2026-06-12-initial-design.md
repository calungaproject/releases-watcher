# Calunga Controller — Pipeline Watchdog for Konflux

## Context

When Python packages are onboarded (via PR merge in a git repo with JSON declarations), a multi-step Konflux pipeline runs. Transient failures (e.g., Quay outages) can silently leave versions unpublished, with no easy way to detect or recover.

**V1 Goal:** Observe-only controller that logs every pipeline state transition, clearly marks failures/stuck pipelines, and sends Slack notifications on pipeline completion or failure.

**Technology:** Python with kopf.

---

## Resource Chain (verified from cluster)

All resources in `calunga-tenant` except release PipelineRun which is in `rhtap-releng-tenant`.

```
Build PipelineRun  ──(annotation: appstudio.openshift.io/snapshot)──▶  Snapshot
      │                                                                    │
      │ label: pipelines.appstudio.openshift.io/type: build                │ label: appstudio.openshift.io/build-pipelinerun
      │                                                                    │
      │                              ┌─────────────────────────────────────┘
      │                              │
      │                              ▼
      │                     Test PipelineRuns (N of them)
      │                       label: pipelines.appstudio.openshift.io/type: test
      │                       label: appstudio.openshift.io/snapshot: <snapshot-name>
      │                              │
      │                              ▼ (all pass)
      │                         Release
      │                           spec.snapshot: <snapshot-name>
      │                           label: release.appstudio.openshift.io/snapshot: <snapshot-name>
      │                              │
      │                              ▼
      │                     Release PipelineRun (in rhtap-releng-tenant)
      │                       label: pipelines.appstudio.openshift.io/type: managed
      │                       label: release.appstudio.openshift.io/name: <release-name>
      │                       label: release.appstudio.openshift.io/namespace: calunga-tenant
```

### Key labels/annotations for correlation

| Label/Annotation | Present on | Purpose |
|---|---|---|
| `pac.test.appstudio.openshift.io/sha` | ALL resources | Git commit SHA — universal correlation key |
| `pac.test.appstudio.openshift.io/sha-title` | ALL resources | Human-readable: e.g., "Automatic build grpcio==1.81.1" |
| `pipelines.appstudio.openshift.io/type` | PipelineRuns | `build`, `test`, or `managed` |
| `appstudio.openshift.io/snapshot` | Test PLRs, Release | Links to Snapshot name |
| `appstudio.openshift.io/build-pipelinerun` | Snapshot, Release | Links back to build PipelineRun name |
| `release.appstudio.openshift.io/name` | Release PLR | Links to Release name |
| `appstudio.openshift.io/application` | ALL resources | Application name (e.g., `calunga-v2-index-main`) |
| `appstudio.openshift.io/component` | Build PLR, Snapshot, Release | Component name |

### GVKs

| Resource | API Group | Version | Kind |
|---|---|---|---|
| PipelineRun | `tekton.dev` | `v1` | `PipelineRun` |
| Snapshot | `appstudio.redhat.com` | `v1alpha1` | `Snapshot` |
| Release | `appstudio.redhat.com` | `v1alpha1` | `Release` |

### Namespaces to watch

- `calunga-tenant` — build PipelineRuns, Snapshots, test PipelineRuns, Releases
- `rhtap-releng-tenant` — release (managed) PipelineRuns

### How to detect pipeline outcome from resources

**Build PipelineRun:** `status.conditions[0].status == "True"` and `reason == "Completed"` → success. `status == "False"` → failure.

**Snapshot:** `status.conditions` with `type: AppStudioTestSucceeded` (status True/False) and `type: AutoReleased` (status True/False).

**Test PipelineRuns:** `status.conditions[0].status == "True"` and `reason == "Succeeded"` → success. Also: Snapshot annotation `test.appstudio.openshift.io/status` has JSON array with per-scenario status.

**Release:** `status.conditions` with `type: Released`, `status: True`, `reason: Succeeded`. The `status.managedProcessing.pipelineRun` field points to the release PipelineRun as `namespace/name`.

**Release PipelineRun:** Same as build — `status.conditions[0]`.

---

## V1 Design: Observe & Log

### What it logs

```
INFO  [grpcio==1.81.1 sha=eb63f46] Build PipelineRun started: calunga-v2-index-main-on-push-2l5j2
INFO  [grpcio==1.81.1 sha=eb63f46] Build PipelineRun succeeded
INFO  [grpcio==1.81.1 sha=eb63f46] Snapshot created: calunga-v2-index-main-20260611-133255-000
INFO  [grpcio==1.81.1 sha=eb63f46] Test started: wheel-check-fedora43 (1/7)
INFO  [grpcio==1.81.1 sha=eb63f46] Test started: wheel-check-ubi8 (2/7)
...
INFO  [grpcio==1.81.1 sha=eb63f46] All 7 tests passed
INFO  [grpcio==1.81.1 sha=eb63f46] Release created: calunga-v2-index-main-...-m4rdk
INFO  [grpcio==1.81.1 sha=eb63f46] Release PipelineRun started: managed-hq8n2 (rhtap-releng-tenant)
INFO  [grpcio==1.81.1 sha=eb63f46] Release PipelineRun succeeded — PIPELINE COMPLETE ✓

ERROR [numpy==1.26.4 sha=abc1234] Build PipelineRun FAILED: calunga-v2-index-main-on-push-xyz — pipeline stuck
ERROR [scipy==1.14.0 sha=def5678] Test FAILED: wheel-check-ubi9 — pipeline stuck (3/7 tests passed, 1 failed)
```

### State tracking (in-memory, rebuilt on startup)

Keyed by git SHA. Each entry tracks:
- Package info (from `sha-title` annotation)
- Current state in the pipeline
- Names of all related resources
- Timestamp of last state change (for stall detection)

### kopf handlers

```python
# Watch build PipelineRuns
@kopf.on.create('tekton.dev', 'v1', 'pipelineruns',
                labels={'pipelines.appstudio.openshift.io/type': 'build'},
                namespace='calunga-tenant')
@kopf.on.update(...)  # same filters
@kopf.on.resume(...)  # rebuild state on startup

# Watch test PipelineRuns
@kopf.on.create('tekton.dev', 'v1', 'pipelineruns',
                labels={'pipelines.appstudio.openshift.io/type': 'test'},
                namespace='calunga-tenant')

# Watch Snapshots
@kopf.on.create('appstudio.redhat.com', 'v1alpha1', 'snapshots',
                namespace='calunga-tenant')

# Watch Releases
@kopf.on.create('appstudio.redhat.com', 'v1alpha1', 'releases',
                namespace='calunga-tenant')

# Watch release PipelineRuns (different namespace!)
@kopf.on.create('tekton.dev', 'v1', 'pipelineruns',
                labels={'pipelines.appstudio.openshift.io/type': 'managed'},
                namespace='rhtap-releng-tenant')
```

---

## Slack Notifications

Send a Slack message on two events:
1. **Pipeline complete** — release PipelineRun succeeded (the whole flow finished)
2. **Pipeline failure** — any step failed (build, test, or release PipelineRun)

Uses the Slack `chat.postMessage` API:

```python
import aiohttp

SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]  # xoxb-...
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "UGZCNQU69")

async def notify_slack(message: str):
    async with aiohttp.ClientSession() as session:
        await session.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            json={"channel": SLACK_CHANNEL, "text": message},
        )
```

**Success message example:**
```
✅ grpcio==1.81.1 (sha=eb63f46) — pipeline complete. Released via managed-hq8n2.
```

**Failure message example:**
```
❌ numpy==1.26.4 (sha=abc1234) — Build PipelineRun FAILED: calunga-v2-index-main-on-push-xyz
```

Token and channel are configured via environment variables (`SLACK_BOT_TOKEN`, `SLACK_CHANNEL`). For local testing, set them in `.env`. For cluster deployment, use a Kubernetes Secret.

---

## Project Structure

```
calunga_controller/
├── pyproject.toml              # kopf, kubernetes, aiohttp dependencies
├── Dockerfile                  # Python 3.12 slim
├── README.md
├── src/
│   └── calunga_controller/
│       ├── __init__.py
│       ├── handlers.py         # kopf event handlers
│       ├── tracker.py          # Pipeline state tracking, correlation
│       ├── config.py           # Namespaces, timeouts, label keys
│       └── slack.py            # Slack notification helper
├── tests/
│   ├── test_tracker.py
│   └── test_handlers.py
└── deploy/
    ├── deployment.yaml
    ├── rbac.yaml
    └── kustomization.yaml
```

---

## Implementation Steps

### Step 1: Project scaffolding
- `pyproject.toml` with `kopf`, `kubernetes`, `aiohttp` dependencies
- `Dockerfile` (Python 3.12 slim)
- `config.py` with constants for label keys, namespaces, GVKs
- Basic kopf entrypoint

### Step 2: Pipeline state tracker
- `tracker.py`: `PipelineTracker` class with in-memory dict keyed by git SHA
- `extract_pipeline_key(resource)` — extract SHA and package info from labels/annotations
- State enum: `BUILD_RUNNING → BUILD_SUCCEEDED → BUILD_FAILED → SNAPSHOT_CREATED → TESTING → TESTS_PASSED → TESTS_FAILED → RELEASING → RELEASED → RELEASE_FAILED`
- Logging on every state transition with package/sha context

### Step 3: Event handlers
- `handlers.py`: kopf handlers for each resource type (as shown above)
- Each handler: extract correlation key → update tracker state → tracker logs the transition
- On failure conditions: log at ERROR level with "pipeline stuck" message

### Step 4: Slack notifications
- `slack.py`: async `notify_slack(message)` using aiohttp
- Called from tracker on pipeline completion (success) or failure at any step
- Token from `SLACK_BOT_TOKEN` env var, channel from `SLACK_CHANNEL` env var

### Step 5: Deployment manifests
- `deploy/deployment.yaml`: single-replica Deployment
- `deploy/rbac.yaml`: ServiceAccount + ClusterRole with get/list/watch on PipelineRuns (both namespaces), Snapshots, Releases
- `deploy/kustomization.yaml`

### Step 6 (optional v1): Stall detection
- `@kopf.timer` on build PipelineRuns: if succeeded but no Snapshot within configurable timeout → WARNING log

---

## Verification

1. **Unit tests**: tracker state machine, correlation key extraction, state transition logging
2. **Deploy to cluster**: `kubectl apply -k deploy/` → watch logs as a real pipeline runs
3. **Failure test**: find/wait for a failed PipelineRun → verify ERROR log appears
4. **Restart test**: kill the controller pod → verify it rebuilds state on restart via `@kopf.on.resume`

---

## Saved Resource Examples

All saved locally for reference during implementation:
- `build-pipelinerun.yaml` — build PipelineRun (from kubearchive, was pruned from cluster)
- `snapshot.yaml` — Snapshot (from cluster)
- `test-pipelinerun.yaml` — one test PipelineRun (from kubearchive)
- `release.yaml` — Release (from cluster)
- `release-pipelinerun.yaml` — release PipelineRun in rhtap-releng-tenant (from kubearchive)
