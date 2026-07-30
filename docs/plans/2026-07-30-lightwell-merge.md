# Plan: Merge `lightwell` branch into `main`

## Context

The `lightwell` branch adapted the watcher for a second project (`pnc-import` / `pnc-import-remediated` on the `stone-prod-p01` cluster). It diverged from `main` before several state-machine guard fixes landed on `main`. The two branches are now ~300 lines apart in `tracker.py` + tests combined.

The good news: **every source-code change in `lightwell` is a generalization**, not a calunga-specific replacement. They can be merged into `main` without breaking the existing calunga workflow. The tricky part is the deploy manifests (environment-specific) and one removed handler (`on_test_pipelinerun`).

---

## What the lightwell branch changed (grouped by nature)

### 1. Source code — pure generalizations (can merge cleanly)

| Change | Notes |
|--------|-------|
| `extract_sha` → `extract_tracking_key` with PLR-name fallback | Adds non-PAC support; PAC (SHA-label) path is unchanged |
| `APPLICATION` → `APPLICATIONS` set (comma-split) | Single-app case becomes a one-element set — no breakage |
| `_app_matches()` helper in handlers | Guards all handlers; replaces label-level filter for snapshots/releases |
| `_snapshot_index` (snapshot name → tracking key) | Needed for non-PAC; harmless for PAC flows |
| Git info extraction (`extract_git_info`) + `PipelineInfo.git_url/git_commit` | Additive; Slack messages show richer info when available |
| `extract_package_title` fallback chain (IMAGE param → component label → name) | Additive fallbacks; `unknown` case now rare |
| `retry_release` reads plan from original release's label, falls back to global | More flexible; existing calunga behavior preserved by fallback |
| `sha_short` widened to 12 chars | Cosmetic |

### 2. Handler removal — the one conflict point

`on_test_pipelinerun` and `TEST_FILTER` were **removed** in `lightwell` because `pnc-import` has no integration-test phase. Calunga still needs this handler.

**Resolution**: Re-add the handler but wrap it with `_app_matches(body)`, exactly like the other handlers. For `pnc-import` it will never fire (no matching label); for calunga it continues to work as before.

### 3. Deploy manifests — environment-specific, keep separate

Currently both branches have a flat `deploy/` directory. The lightwell branch renames every resource and changes namespaces/secrets/image tags. These cannot be in the same flat directory.

**Resolution**: Reorganize into:
```
deploy/
  calunga/           # existing calunga manifests (unchanged)
    configmap.yaml
    deployment.yaml
    kustomization.yaml
  lightwell/          # new lightwell manifests
    configmap.yaml
    deployment.yaml
    kustomization.yaml
```

---

## Single image, two deployments — how the config differs

With a unified codebase, both deployments run the **same image**. Since both run on the same cluster, the proxy settings are shared too — only the `NO_PROXY` value differs (lightwell adds the remote API endpoint for `stone-prod-p01`). All environment differences live in `ConfigMap` env vars:

| Env variable | calunga | lightwell |
|---|---|---|
| `TENANT_NAMESPACE` | `calunga-tenant` | `lightwell-poc-tenant` |
| `APPLICATION` | `calunga-v2-index-main` | `pnc-import,pnc-import-remediated` |
| `RELEASE_PLAN` | _(removed — always read from Release label)_ | _(same)_ |
| `RETRY_ENABLED` | `true` | `false` |
| `AI_MODEL` | `claude-haiku-4-5-20251001` | `claude-sonnet-4-6` |
| `NO_PROXY` | `.svc,.cluster.local,10.0.0.0/8,172.0.0.0/8` | same + `,api.stone-prod-p01.wcfb.p1.openshiftapps.com` |
| `SLACK_CHANNEL` | calunga Slack channel | lightwell Slack channel |
| `GOOGLE_CLOUD_PROJECT` | calunga GCP project | lightwell GCP project |

The `Deployment` manifests also differ in: deployment name/labels, secret name, and memory limits. `HTTPS_PROXY` and `HTTP_PROXY` are the same on both.

### ArgoCD / Kustomize layout: base + overlays

Rather than two flat copies, we use one Kustomize base with two overlays — the standard pattern for ArgoCD `Application` resources:

```
deploy/
  base/
    deployment.yaml      # name: release-watcher, refs configMapRef: release-watcher-config
    configmap.yaml       # common defaults (AI settings, proxy, etc.)
    kustomization.yaml   # resources: [deployment.yaml, configmap.yaml]
  overlays/
    calunga/
      kustomization.yaml          # namePrefix: calunga-, namespace: calunga--runtime-int
      configmap-patch.yaml        # TENANT_NAMESPACE, APPLICATION, AI_MODEL, RETRY_ENABLED
      deployment-patch.yaml       # secret name, paas label, memory limits, NO_PROXY
    lightwell/
      kustomization.yaml          # namePrefix: lightwell-, namespace: <lightwell ns>
      configmap-patch.yaml        # TENANT_NAMESPACE, APPLICATION, AI_MODEL, RETRY_ENABLED
      deployment-patch.yaml       # secret name, memory limits, NO_PROXY extra entry
```

Each ArgoCD `Application` points to its overlay path. The base contains the shared structure; overlays only patch what differs.

### What `RELEASE_PLAN=""` actually means — with examples

**Already partially implemented in `lightwell`**, but we will go one step further: **drop the `RELEASE_PLAN` env var entirely** from `retrier.py`. The lightwell version still falls back to the global `RELEASE_PLAN`; we will remove that fallback.

Current lightwell code in `retrier.py`:
```python
release_plan = orig_labels.get(LBL_RELEASE_PLAN, "") or RELEASE_PLAN  # still falls back
```

Proposed merged code:
```python
release_plan = orig_labels.get(LBL_RELEASE_PLAN)
if not release_plan:
    logger.error("%s Original release has no %s label, cannot determine retry plan",
                 info.log_prefix, LBL_RELEASE_PLAN)
    return None
```

This means `RELEASE_PLAN` is removed from `config.py`, both configmaps, and `retrier.py`. `LBL_RELEASE_PLAN` (`release.appstudio.openshift.io/releasePlan`) is a standard AppStudio label that the Release Service sets on every Release object, so it will always be present.

In the current `main` branch, `retry_release` always uses the global `RELEASE_PLAN` env var (`"calunga"`). This works for calunga because there is one app, one plan.

`pnc-import` and `pnc-import-remediated` can have **different** release plans. Rather than adding per-app env vars, reading from the **original Release object's own label** gives exact self-describing behaviour:

```yaml
# Original Release (created by the integration test service, not by us):
apiVersion: appstudio.redhat.com/v1alpha1
kind: Release
metadata:
  name: pnc-import-abc123
  labels:
    appstudio.openshift.io/application: pnc-import
    appstudio.openshift.io/release-plan: pnc-import-java-pulp-validated-prod
```

When a retry is needed, `retry_release` reads `body["metadata"]["labels"]["appstudio.openshift.io/release-plan"]` and produces:

```yaml
# Retried Release (created by the watcher):
apiVersion: appstudio.redhat.com/v1alpha1
kind: Release
metadata:
  generateName: pnc-import-retry-
  labels:
    appstudio.openshift.io/release-plan: pnc-import-java-pulp-validated-prod  # copied from original
spec:
  snapshot: the-same-snapshot
  releasePlan: pnc-import-java-pulp-validated-prod   # from the label, not the env var
```

Every Release object in AppStudio carries `LBL_RELEASE_PLAN` (set by the Release Service at creation time), so no fallback is needed. `RELEASE_PLAN` is removed entirely — from `config.py`, `retrier.py`, and both configmaps.

---

## Recommended approach: replay lightwell changes on top of `main`

A straight `git merge lightwell` will produce conflicts throughout `tracker.py` and the test files because both branches edited those files independently. The lightwell changes are small and well-understood, so replaying them manually is cleaner.

### Step-by-step plan

**Phase 1: config.py**
- Add `APPLICATIONS = {app.strip() for app in APPLICATION.split(",")}` derived constant
- Restore calunga defaults (`TENANT_NAMESPACE = "calunga-tenant"`, `APPLICATION = "calunga-v2-index-main"`)
- Remove `RELEASE_PLAN` entirely

**Phase 2: tracker.py**
- Rename `extract_sha` → `extract_tracking_key` with PLR-name fallback (keep SHA-label path first)
- Add `extract_git_info(body)` function
- Add `git_url`, `git_commit` fields to `PipelineInfo`; add `git_commit_short`, `git_summary` properties
- Add `_snapshot_index: dict[str, str]` to `PipelineTracker`
- Rename `_seen_shas` → `_seen_keys`, update all usages
- Extend `extract_package_title` with IMAGE-param → component-label → name fallback chain
- Update Slack messages to use `info.git_summary`
- Update `on_snapshot` to populate `_snapshot_index` and call `extract_git_info`
- Update `on_build_pipelinerun` to use PLR-name fallback key and call `extract_git_info` on success
- Update `on_release` / `on_release_pipelinerun` to use `_snapshot_index` fallback lookup

**Phase 3: handlers.py**
- Import `APPLICATIONS` instead of `APPLICATION`
- Add `_app_matches(body)` helper
- Simplify `BUILD_FILTER` (drop event-type label)
- Update `SNAPSHOT_FILTER` and `RELEASE_FILTER` to remove label selectors (guard inside handler)
- Keep `on_test_pipelinerun` but add `_app_matches` guard at the top
- Update startup log to show `sorted(APPLICATIONS)`

**Phase 4: retrier.py**
- In `retry_release`: read `release_plan` from `orig_labels.get(LBL_RELEASE_PLAN)` only; log error and return `None` if absent; remove `RELEASE_PLAN` import

**Phase 5: deploy manifests**
- Move existing `deploy/*.yaml` → `deploy/calunga/`
- Create `deploy/lightwell/` with the lightwell-specific configmap and deployment (from the branch)
- Update `README.md` to document the new layout

**Phase 6: tests**
- Update `test_config.py` for the new `APPLICATIONS` set and comma-split behavior
- Update `test_tracker.py`: rename `TestExtractSha` → `TestExtractTrackingKey`, add PLR-name fallback test, add `TestExtractGitInfo`, add `TestNonPacFlow`, update `sha_short` to 12 chars
- Update `test_retrier.py`: rename test, assert plan comes from label
- Run `pytest` to confirm all existing tests still pass with calunga defaults

---

## What NOT to change

- State machine states and transition guards (main branch has fixes not in lightwell)
- `processed_failure_sources` dedup logic (main-only fix)
- `_RETRYING_BLOCKED_TRANSITIONS` (main-only fix)
- `k8s.py`, `analyzer.py`, `slack.py` (unchanged in lightwell)

---

## Verification

```bash
# Unit tests with calunga defaults — must all pass
pytest tests/unit/ -v

# Confirm APPLICATIONS set works for both single and comma-separated
APPLICATION="calunga-v2-index-main" python -c "from calunga_release_watcher.config import APPLICATIONS; print(APPLICATIONS)"
APPLICATION="pnc-import,pnc-import-remediated" python -c "from calunga_release_watcher.config import APPLICATIONS; print(APPLICATIONS)"

# Coverage check
pytest tests/unit/ --cov=calunga_release_watcher --cov-report=term-missing
```

Expected: all unit tests green, coverage ≥ 88%.
