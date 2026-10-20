# Argo CD applications

This directory defines the Applications that deploy the Calunga and Lightwell
release watchers into `calunga--runtime-int`. Both use Argo CD's existing
`default` project. It assumes a namespaced Argo CD instance has already been
installed separately; the Argo CD instance itself is intentionally not managed
by this repository.

## Prerequisite secrets

Secrets are intentionally not stored in Git. Provision these secrets in
`calunga--runtime-int` through the platform's secret-management workflow before
creating the Applications:

| Secret | Required keys |
|---|---|
| `calunga-release-watcher-secrets` | `k8s-token`, `slack-token`, `slack-channel`, `gcp-sa-key` |
| `lightwell-release-watcher-secret` | `k8s-token`, `slack-token`, `slack-channel`, `gcp-sa-key` |

The Kubernetes tokens authenticate to the remote clusters named by each
overlay's `K8S_API_URL`; they are not service-account tokens for DNO.

### Lightwell image metadata access (p01 prerequisite)

Lightwell GAV enrichment through `image-rbac-proxy` requires the
watcher's p01 service account, `lightwell-poc-tenant/konflux-bot-0`, to have read
access to `imagerepositories.appstudio.redhat.com` in `lightwell-poc-tenant`.
The proxy uses this permission to allow access to all component image
repositories in that tenant. The existing p01 token used by the DNO watcher
can authenticate to the proxy; no Quay pull secret needs to be copied.

The prerequisite RoleBinding is defined **for p01**, separately from the
DNO Argo applications. It binds the existing
`konflux-externalpuller-bot-actions` ClusterRole to `konflux-bot-0` only within
`lightwell-poc-tenant`:

```sh
KUBECONFIG=/home/rhopp/temp/p01.kubeconfig \
  kubectl apply -f deploy/prerequisites/p01/lightwell-image-reader.yaml
```

The ClusterRole grants `get`, `list`, and `watch` on
`imagerepositories.appstudio.redhat.com`. This binding follows p01's policy:
service accounts with the `konflux-bot-` prefix can only be bound to
ClusterRoles matching `konflux-*-bot-actions`, not to custom Roles.

The proxy endpoint is
`https://image-rbac-proxy.apps.stone-prod-p01.wcfb.p1.openshiftapps.com`.
GAV values are OCI manifest annotations (for example,
`dev.lightwell.primary-gav`), so fetching the manifest does not require
downloading artifact layers. The Lightwell overlay enables enrichment with
`IMAGE_METADATA_PROXY_URL`; leaving it unset disables lookups (the Calunga
default). The watcher uses its existing `K8S_TOKEN` to fetch the single-component
snapshot's digest-pinned input manifest when sending a release success or failure message.
Images are selected by the snapshot referenced by the release, rather than
whichever snapshot event arrived last. If that snapshot has not been observed,
the watcher reads it from the tenant API before attempting enrichment; an
unavailable snapshot retains the existing title.
The primary GAV replaces the package/source title; a GAV count greater than one
adds an additional-artifact count. Multi-component snapshots retain the existing
title. Missing annotations, rejected access, or lookup failures also retain the
existing message. Requests use TLS verification, a five-second timeout, a 1 MiB
response limit, and do not follow redirects or download layers.

## Deploy the Argo applications

Each overlay sets `KONFLUX_UI_URL` to its source cluster's Konflux UI.
Release success and release failure messages link to the managed PipelineRun
using its own namespace and name. If the UI URL or PipelineRun reference is
unavailable, messages retain their existing text without a link. Build and
test failure messages do not acquire release-pipeline links. Links may stop
resolving after PipelineRun retention removes the resource, depending on the
UI's archive support.
Before sending a terminal release notification without a PipelineRun reference,
the watcher re-reads that Release once for the latest reference. Context API
reads use a five-second timeout. If the reference remains unavailable (for
example, validation failed before any pipeline was created), the notification
is delivered without a link rather than waiting indefinitely.

After each Application's configured Git revision is available remotely, apply
the desired Application in the namespace where the external Argo CD instance
runs:

```sh
KUBECONFIG=/home/rhopp/temp/dno.kubeconfig \
  kubectl apply -k deploy/argocd
```

Keep the source-cluster deployments running until both DNO watchers have
completed their initial 60-second sync and are processing new events. Then stop
the source deployments to avoid duplicate notifications and automated retries.
