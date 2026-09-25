# Calunga Release Watcher

A Kubernetes controller that watches the CI/CD pipeline lifecycle in
[Konflux / AppStudio](https://konflux-ci.dev/) and sends Slack
notifications on failures and successful releases.

When a pipeline fails, the controller can optionally run AI-powered
failure analysis using Claude via Google Vertex AI to classify
the failure and suggest next steps. When the failure is classified as
a transient "fluke," the controller can automatically retry the failed
step (see [Automatic retries](#automatic-retries)).

## Pipeline lifecycle

Every code push triggers a pipeline that moves through these stages:

```
BUILD_RUNNING ─► BUILD_SUCCEEDED ─► SNAPSHOT_CREATED ─► TESTING ─► TESTS_PASSED ─► RELEASING ─► RELEASED
      │                                                    │                           │
      ▼                                                    ▼                           ▼
 BUILD_FAILED                                         TESTS_FAILED              RELEASE_FAILED
      │                                                    │                           │
      ▼                                                    ▼                           ▼
BUILD_RETRYING                                      TESTS_RETRYING           RELEASE_RETRYING
```

Once a pipeline reaches a terminal state (`RELEASED` or any `*_FAILED`
state), further updates for that commit SHA are ignored. `*_RETRYING`
states are non-terminal — they allow the pipeline to continue when
retry attempt resources arrive.

## How it works

The controller uses [kopf](https://kopf.readthedocs.io/) to watch five
types of Kubernetes resources, all filtered by application label:

| Resource | Namespace | What it represents |
|---|---|---|
| `PipelineRun` (type=build) | tenant | The build step |
| `Snapshot` | tenant | A snapshot of the built artifact |
| `PipelineRun` (type=test) | tenant | Integration test runs |
| `Release` | tenant | The release request |
| `PipelineRun` (type=managed) | release | The release pipeline that does the actual work |

Each Kubernetes event is routed to the `PipelineTracker`, which maintains
an in-memory map of commit SHA to pipeline state. State transitions
trigger log messages and, for terminal states, Slack notifications.

### Startup behavior

On startup kopf "resumes" all existing resources. The tracker absorbs
these silently during a **60-second grace period** (`SYNC_GRACE_PERIOD`),
then calls `set_live()` which prunes already-finished and stale
pipelines and enables notifications going forward. SHAs seen during
the initial sync are remembered so that late watch re-deliveries of
old resources cannot create spurious notifications.

`SLACK_BOT_TOKEN` and `SLACK_CHANNEL` are validated at startup — the
pod will crash if either is missing.

### AI failure analysis

When `AI_ANALYSIS_ENABLED` is `true` and a pipeline fails, the analyzer:

1. Gathers context from the Kubernetes cluster (TaskRun conditions and
   pod logs from failed containers)
2. For test failures (detected via Snapshot), looks up all test
   PipelineRuns and aggregates results across all failed tests
3. Sends the context to Claude via Google Vertex AI
4. Enriches the Slack notification with:
   - **Classification**: `fluke` (transient), `real` (genuine issue),
     `infra` (infrastructure), or `unknown`
   - **Confidence**: high, medium, or low
   - **Root cause**: 1-2 sentence explanation
   - **Suggestion**: actionable recommendation

AI analysis failures are non-blocking — the basic Slack notification
is always sent even if AI analysis fails.

### Automatic retries

When `RETRY_ENABLED` is `true` and the AI classifies a failure as
`fluke` with confidence meeting `RETRY_CONFIDENCE_THRESHOLD`, the
controller automatically retries:

- **Test failure**: patches the Snapshot with `test.appstudio.openshift.io/run`
  label, which triggers the integration test service to re-run the
  failed scenario(s).
- **Release failure**: creates a new `Release` object in `TENANT_NAMESPACE`
  with the same snapshot and release plan.
- **Build failure**: not yet implemented (requires replicating PAC behavior).

Retry attempts are tracked in-memory (up to `MAX_RETRIES` per pipeline).
A threaded Slack reply is sent for each retry attempt and when retries
are exhausted.

### Proxy support

The controller runs on an OCP hub cluster but watches resources on a
remote cluster. If that cluster is only reachable through an HTTP proxy,
set the standard proxy environment variables on the pod:

| Variable | Description |
|---|---|
| `HTTPS_PROXY` / `HTTP_PROXY` | Proxy URL, e.g. `http://proxy.example.com:3128` |
| `NO_PROXY` | Comma-separated hosts/CIDRs to bypass (e.g. `.svc,.cluster.local,10.0.0.0/8`) |

The controller sets `trust_env=True` on kopf's aiohttp session and on
the `ConnectionInfo` returned by the login handler, so kopf respects
these variables for all Kubernetes API watch/list connections.

## Project structure

```
src/calunga_release_watcher/
  config.py      — Environment-variable configuration and Kubernetes label/annotation constants
  handlers.py    — kopf event handlers (wiring between Kubernetes events and the tracker)
  tracker.py     — Core state machine: PipelineTracker, PipelineInfo, and helper functions
  slack.py       — Sends Slack notifications via the Slack Web API
  analyzer.py    — AI-powered failure analysis via Claude on Vertex AI
  retrier.py     — Automatic retry logic for fluke failures
  k8s.py         — Kubernetes client initialization
```

## Tests

Install dev dependencies:

```bash
pip install -e '.[dev]'
```

### Unit tests

Unit tests mock all external dependencies (Kubernetes API, Slack, Vertex AI)
and run without any credentials or network access.

```bash
pytest tests/unit/ -v
```

With coverage report:

```bash
pytest tests/unit/ -v --cov=calunga_release_watcher --cov-report=term-missing
```

### E2E tests

E2E tests exercise the full `analyze_failure` pipeline: they load real
Kubernetes resource fixtures captured from kubearchive, mock the k8s API
to serve this canned data, and send the assembled context to a **real
Claude model via Vertex AI**. Assertions are soft — checking that the
classification falls within an acceptable set rather than exact matching.

Requirements:
- `ANTHROPIC_VERTEX_PROJECT_ID` or `GOOGLE_CLOUD_PROJECT` env var set
- `CLOUD_ML_REGION` env var (defaults to `global`) — **e2e tests only**;
  the deployed app uses `GOOGLE_CLOUD_REGION`
- Valid GCP credentials (`gcloud auth application-default login`)

```bash
pytest tests/e2e/ -v -m e2e
```

Without Vertex credentials, e2e tests are automatically skipped.

### Running all tests

```bash
pytest tests/ -v
```

## Deployment

The two watcher variants are rendered from a shared Kustomize base:

```sh
kubectl kustomize deploy/overlays/calunga
kubectl kustomize deploy/overlays/lightwell
```

The Applications used to deploy these overlays are under `deploy/argocd` and
use Argo CD's `default` project. The Argo CD instance itself is installed and
managed outside this repository. See
[`deploy/argocd/README.md`](deploy/argocd/README.md) for prerequisites and the
cutover procedure.

## Configuration

All configuration is via environment variables.

### Core

| Variable | Default | Description |
|---|---|---|
| `TENANT_NAMESPACE` | `calunga-tenant` | Namespace where builds, tests, snapshots, and releases live |
| `RELEASE_NAMESPACE` | `rhtap-releng-tenant` | Namespace where release PipelineRuns run |
| `APPLICATION` | `calunga-v2-index-main` | AppStudio application name to filter resources by |
| `SLACK_BOT_TOKEN` | *(empty)* | **Required.** Slack Bot token. Pod fails on startup if not set |
| `SLACK_CHANNEL` | *(empty)* | **Required.** Slack channel ID. Pod fails on startup if not set |
| `K8S_TOKEN` | *(empty)* | Kubernetes bearer token for remote cluster auth |
| `K8S_API_URL` | *(empty)* | Kubernetes API server URL. If unset, uses in-cluster config |
| `MAX_RETRIES` | `3` | Maximum automatic retry attempts per pipeline |

### AI failure analysis

| Variable | Default | Description |
|---|---|---|
| `AI_ANALYSIS_ENABLED` | `false` | Set to `true` to enable AI failure analysis |
| `GOOGLE_CLOUD_PROJECT` | *(empty)* | GCP project ID for Vertex AI. Required when AI is enabled |
| `GOOGLE_CLOUD_REGION` | `global` | GCP region for Vertex AI |
| `AI_MODEL` | `claude-haiku-4-5` | Anthropic model ID (deployed configmap uses `claude-haiku-4-5-20251001`) |
| `AI_MAX_LOG_LINES` | `200` | Maximum log lines to fetch per container |
| `AI_TIMEOUT_SECONDS` | `30` | Timeout for AI API calls |
| `GOOGLE_APPLICATION_CREDENTIALS` | *(unset)* | Path to GCP service account key file for Vertex AI auth |

### Automatic retries

| Variable | Default | Description |
|---|---|---|
| `RETRY_ENABLED` | `false` | Set to `true` to enable automatic retries for fluke failures |
| `RETRY_CONFIDENCE_THRESHOLD` | `medium` | Minimum AI confidence to trigger retry (`low`/`medium`/`high`) |
| `RELEASE_PLAN` | `calunga` | Release plan name used when creating retry Release objects |
