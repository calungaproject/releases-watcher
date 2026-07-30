# Calunga Controller — Session Notes (2026-06-11)

## What We Built

A Kubernetes controller ("Calunga Controller") that watches Konflux CI pipeline resources in real time to detect failures in multi-step pipelines. Written in Python using **kopf** (Kubernetes Operator Pythonic Framework).

**V1 scope: observe and log only** — no automatic retries. Logs every pipeline state transition, sends Slack notifications on pipeline completion or failure.

The controller tracks both **package version updates** (e.g., "Automatic build grpcio==1.81.1") and **new package onboarding** (e.g., "Onboard package openai-agents") — both go through the same pipeline.

---

## The Konflux Pipeline Being Watched

```
Build PipelineRun (calunga-tenant)
        │
        ▼
    Snapshot (calunga-tenant)
        │
        ▼
    Test PipelineRuns ×N (calunga-tenant)
        │  (all must pass)
        ▼
    Release (calunga-tenant)
        │
        ▼
    Release PipelineRun (rhtap-releng-tenant)  ← different namespace!
        │
        ▼
    Pulp publish (wheels available)
```

Each step produces a Kubernetes custom resource. All resources are correlated by git SHA (`pac.test.appstudio.openshift.io/sha` label/annotation).

### Resource Types (GVKs)

| Resource    | API Group              | Version   | Namespace             |
|-------------|------------------------|-----------|-----------------------|
| PipelineRun | `tekton.dev`           | `v1`      | both namespaces       |
| Snapshot    | `appstudio.redhat.com` | `v1alpha1`| `calunga-tenant`      |
| Release     | `appstudio.redhat.com` | `v1alpha1`| `calunga-tenant`      |

### Pipeline type distinguished by label

`pipelines.appstudio.openshift.io/type`: `build`, `test`, or `managed`

### Key labels/annotations for correlation

| Key | Present on | Purpose |
|-----|-----------|---------|
| `pac.test.appstudio.openshift.io/sha` | ALL | Git SHA — universal correlation key |
| `pac.test.appstudio.openshift.io/sha-title` | ALL | Human-readable title (e.g., "Automatic build grpcio==1.81.1") |
| `pipelines.appstudio.openshift.io/type` | PipelineRuns | `build`, `test`, or `managed` |
| `appstudio.openshift.io/snapshot` | Test PLRs, Release | Links to Snapshot name |
| `appstudio.openshift.io/build-pipelinerun` | Snapshot | Links back to build PLR |
| `release.appstudio.openshift.io/name` | Release PLR | Links to Release name |

### How to detect outcomes

- **Build PipelineRun**: `status.conditions[0]` — `status=True/False`, `reason=Completed/Failed`
- **Snapshot**: Named conditions — `AppStudioTestSucceeded`, `AutoReleased`
- **Test PipelineRuns**: `status.conditions[0]` — same as build
- **Release**: Named condition `Released` with `status=True/False`
- **Release PipelineRun**: `status.conditions[0]` — same as build

---

## Architecture Decisions

### Why Python/kopf over Go/controller-runtime

- Team has Python expertise, not Go
- Internal tool, not high-throughput infrastructure
- Future plan: plug in LLM for failure classification — Python ecosystem is stronger
- kopf handles the hard parts (watch streams, reconnection, leader election)
- Handles tens/hundreds of concurrent pipelines fine

### Stateless reconciliation

State is derived from current cluster resources, not event history. On pod eviction/restart, `@kopf.on.resume` replays existing resources to rebuild state. This is a fundamental K8s controller pattern.

### Remote cluster watching

The controller runs on one cluster but watches resources on the Konflux production cluster remotely. This was necessary because we don't have Deployment creation rights on the production cluster. Uses a custom `@kopf.on.login()` handler that reads `K8S_API_URL` and `K8S_TOKEN` env vars to connect to the remote cluster.

### ServiceAccount: konflux-bot-0

Uses the existing `konflux-bot-0` SA in `calunga-tenant` on rh03. This SA has:
- **get/list/watch** on PipelineRuns, Snapshots, Releases in both `calunga-tenant` and `rhtap-releng-tenant` (via `konflux-viewer-user-actions` ClusterRole)
- **NO** Events or Leases access — worked around with kopf settings:
  - `settings.posting.enabled = False` (skip K8s Events)
  - `settings.peering.standalone = True` (skip leader election Leases)
- These are fine for a single-replica observer. Don't scale to multiple replicas without adding Lease access.

---

## Key kopf Lessons Learned

### 1. Namespace filtering
kopf handlers don't accept a `namespace` parameter directly. Use `when=` callback filter:
```python
def _in_namespace(ns: str):
    return lambda namespace, **_: namespace == ns

@kopf.on.create(..., when=_in_namespace("calunga-tenant"))
```

### 2. CRD scanning must be disabled
kopf auto-scans CRDs cluster-wide on startup. Without cluster-wide CRD list access, this fails with 9 retries. Fix: `settings.scanning.disabled = True`

### 3. Finalizers must be disabled for read-only controllers
kopf writes `kopf.zalando.org/*` annotations on watched objects by default. For a read-only controller: `settings.persistence.finalizer = ""`

### 4. Resume handlers replay ALL existing resources on startup
`@kopf.on.resume` processes every existing resource as if it's new — floods logs and triggers notifications for historical events. **Critical fix**: separate resume handlers from create/update handlers. Resume handlers populate state silently; create/update handlers log and notify. A `_live` flag in the tracker controls this:
- During startup (15s grace period): `_live = False` — state is populated but no logging/Slack
- After grace period: `_live = True` — normal operation

### 5. Stale resources from pruning
Many PipelineRuns are pruned from the cluster. Snapshots/Releases survive longer. On startup, the controller sees Snapshots for pipelines whose build PLR is long gone — these look "in-progress" but are actually stale. Fix: in `set_live()`, discard any pipeline that has no `build_pipelinerun` set (it was never seen starting).

### 6. Async in thread pools doesn't work
kopf runs handlers in thread pool threads. `asyncio.ensure_future()` fails with "no current event loop in thread". Fix: use synchronous stdlib `urllib.request` for Slack, called from daemon threads via `threading.Thread(target=..., daemon=True).start()`.

### 7. `--namespace` flag required
Without `--namespace=X` on the CLI, kopf defaults to cluster-wide watching and logs a FutureWarning. Explicitly pass both namespaces in the Dockerfile CMD.

### 8. Startup handlers complete before watches begin
`@kopf.on.startup()` runs as an "activity" — it completes before kopf opens watch streams. An `asyncio.sleep(5)` in an async startup handler finishes before any resume handler fires. The working approach: spawn a background daemon thread with `time.sleep(SYNC_GRACE_PERIOD)` from the synchronous startup handler.

---

## kubearchive

Resources pruned from the cluster can be fetched from kubearchive:
```
<kubearchive-api-url>
```
Uses the same SA token. API follows standard K8s REST paths:
```
/apis/tekton.dev/v1/namespaces/{ns}/pipelineruns/{name}
/apis/tekton.dev/v1/namespaces/{ns}/taskruns/{name}
```

---

## Deployment Details

### Where it runs
- **Cluster**: rhtap-services
- **Namespace**: `calunga-controller` (created for this)
- **Watches**: Konflux production cluster remotely

### Image
`quay.io/rhopp/calunga-controller:latest` — built with `podman build`, pushed to quay.io/rhopp (public)

### Secrets (in `calunga-controller` namespace on rhtap-services)
- `calunga-controller-secrets`:
  - `k8s-token` — long-lived SA token for the watched cluster
  - `slack-token` — Slack bot token
- `calunga-controller-slack` — also exists in `calunga-tenant` on rh03 from an earlier attempt, can be cleaned up

### Resource limits
Initial 256Mi was OOMKilled during resume (processing ~2300 resources). Bumped to 512Mi limit / 256Mi request.

### Deploy commands
```bash
# Build and push
podman build -t quay.io/rhopp/calunga-controller:latest .
podman push quay.io/rhopp/calunga-controller:latest

# Deploy (on rhtap-services cluster)
kubectl apply -f deploy/deployment.yaml -n calunga-controller

# Restart after image update
kubectl rollout restart deployment/calunga-controller -n calunga-controller

# Check logs
kubectl logs -f -n calunga-controller deploy/calunga-controller
```

---

## What the 6 Failed Releases Were (June 4 & 9)

Investigated during this session. All 6 had `Release processing failed on managed pipelineRun`:

**5 failed on `create-advisory` task** (June 4, within 3 min window):
- google-cloud-audit-log==0.6.0, ty==0.0.43, plotly==6.8.0, google-api-core==2.31.0, boto3-stubs==1.43.22
- The `run-script` step in `create-advisory` exited code 1 with empty `advisory_url`
- **Pulp upload (`push-py-pulp`) succeeded** — wheels were published, only advisory failed
- Likely a transient infrastructure issue with the advisory service

**1 failed on `push-py-pulp` task** (June 9):
- openai-agents (new package onboarding)
- The `upload` step itself failed — wheels were **NOT** published

---

## File Structure

```
calunga_controller/
├── pyproject.toml              # kopf>=1.44, kubernetes>=28.0
├── Dockerfile                  # python:3.12-slim, kopf run with --namespace flags
├── PLAN.md                     # Original plan document
├── SESSION_NOTES.md            # This file
├── .env                        # Local tokens (DO NOT COMMIT)
├── src/calunga_controller/
│   ├── __init__.py
│   ├── config.py               # Env vars, label/annotation key constants
│   ├── handlers.py             # kopf event handlers + startup + remote login
│   ├── tracker.py              # PipelineTracker state machine, Slack notifications
│   └── slack.py                # Synchronous Slack via urllib.request
├── deploy/
│   ├── deployment.yaml         # Deployment (no SA — uses default, connects remotely)
│   ├── rbac.yaml               # SA + ClusterRole (NOT USED — kept for reference if deploying on rh03 directly)
│   └── kustomization.yaml      # Only references deployment.yaml
├── tests/
│   ├── test_tracker.py
│   └── test_handlers.py
└── *.yaml                      # Saved resource examples (build-pipelinerun.yaml, snapshot.yaml, etc.)
```

---

## Future Work

1. **Automatic retries** — re-trigger failed pipelines (needs write access)
2. **LLM-based failure classification** — analyze failure logs to distinguish transient vs. real failures
3. **Stall detection** — `@kopf.timer` to flag pipelines stuck at a stage too long
4. **Token rotation** — the konflux-bot-0 token expires ~2027-06, needs a rotation mechanism
5. **kubearchive integration** — query kubearchive for pruned resources to get full pipeline history
6. **Cleanup** — delete `calunga-controller-slack` secret from `calunga-tenant` on rh03 (leftover from earlier attempt)
