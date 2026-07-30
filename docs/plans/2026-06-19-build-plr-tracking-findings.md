# Watcher Findings — 2026-06-19

Investigation of calunga-release-watcher behavior during hypothesis==6.155.5 (sha=c7dffe3) release.

## 1. Build pipeline not registered

**Root cause: label key mismatch in BUILD_FILTER**

The build PipelineRun `calunga-v2-index-main-on-push-84b5l` uses the Pipelines-as-Code label domain:

```
pipelinesascode.tekton.dev/event-type: push
```

But `BUILD_FILTER` in `src/calunga_release_watcher/handlers.py:45` uses `LBL_EVENT_TYPE` from `config.py:34`:

```
pac.test.appstudio.openshift.io/event-type: push
```

These are different label keys. The build PLR never matched the filter, so `on_build_pipelinerun` was never called. The state machine skipped straight to `SNAPSHOT_CREATED` when the Snapshot event arrived.

The build PLR was created at 18:14:22 and completed at 18:16:22 — well within the watcher's lifetime (started at 14:34). It was not a startup timing issue.

**Confirmed via kubearchive** (PLR was already pruned from the cluster):
```
TOKEN=$(KUBECONFIG=/home/rhopp/temp/rh03.kubeconfig kubectl config view --minify --raw -o jsonpath='{.users[0].user.token}')
curl -sk -H "Authorization: Bearer $TOKEN" \
  "https://kubearchive-api-server-product-kubearchive.apps.kflux-prd-rh03.nnv1.p1.openshiftapps.com/apis/tekton.dev/v1/namespaces/calunga-tenant/pipelineruns/calunga-v2-index-main-on-push-84b5l"
```

**TODO:** Check which event-type label the test PipelineRuns carry — they might use `pac.test.appstudio.openshift.io/event-type` (which would explain why tests work but builds don't). Then decide: use a separate label constant for build PLRs, or drop the event-type filter from BUILD_FILTER entirely (since the `pipelines.appstudio.openshift.io/type: build` label already scopes it).

## 2. Excessive duplicate logging — three causes

### a) `on_test_pipelinerun` logs outside `_transition()`

`tracker.py:304-313` — every time a test PLR event arrives with `status == "True"`, it logs `"Test passed: X (N/N)"` unconditionally. The `_transition()` dedup guard (`if new_state == old_state: return`) doesn't protect this log statement. A single completed test PLR receiving 4 Kubernetes events produces 4 identical "Test passed" lines.

Same issue at `tracker.py:365-371` — `on_release_pipelinerun` logs "Release PipelineRun started" on every event where `status is None`, not just the first time.

**Fix:** Gate these log lines behind a "first time" check, e.g. track which test scenarios have already been logged as passed.

### b) kopf `@kopf.on.event` fires on every resource mutation

All handlers use `@kopf.on.event()` (not `@kopf.on.update()` with idempotency). Every Kubernetes event (status condition change, annotation update, resync) triggers the handler. The `_transition()` dedup prevents duplicate state changes but the handler still runs and kopf still logs `Handler 'on_release_pipelinerun' succeeded` each time.

**Fix:** Set `logging.getLogger("kopf.objects").setLevel(logging.WARNING)` to suppress the per-event kopf noise.

### c) Snapshot/test event cascade

Each test PipelineRun completion also triggers a Snapshot update (Konflux reconciler updates Snapshot conditions). This causes interleaved pairs: test PLR event → log + snapshot event → log with snapshot_created + possibly re-logs test info.

### d) Health check noise

`aiohttp.access` logs every 30-second kube-probe GET /healthz. After the release pipeline is done, the logs are 100% healthcheck lines.

**Fix:** `logging.getLogger("aiohttp.access").setLevel(logging.WARNING)`

## Summary of fixes needed

| Priority | File | Issue | Fix |
|----------|------|-------|-----|
| P0 | config.py / handlers.py | BUILD_FILTER uses wrong event-type label key | Use `pipelinesascode.tekton.dev/event-type` for build PLRs, or drop event-type from BUILD_FILTER |
| P1 | tracker.py:304-313 | "Test passed" logged on every event, not just first | Track already-logged test completions |
| P1 | tracker.py:365-371 | "Release PLR started" logged on every event | Track whether start has been logged |
| P2 | handlers.py (startup) | kopf.objects INFO spam | Set kopf.objects logger to WARNING |
| P2 | handlers.py (startup) | aiohttp.access healthcheck spam | Set aiohttp.access logger to WARNING |
