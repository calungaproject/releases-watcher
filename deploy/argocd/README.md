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
