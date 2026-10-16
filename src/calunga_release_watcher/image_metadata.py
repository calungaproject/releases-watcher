"""Read Lightwell GAV annotations through the tenant's registry proxy."""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from calunga_release_watcher.config import IMAGE_METADATA_PROXY_URL, TENANT_NAMESPACE

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Kubernetes token to a redirect destination.
        return None


def get_gav(image: str) -> str:
    """Return a display GAV, or empty text when enrichment is unavailable.

    Only digest-pinned images in this tenant's Quay organization are allowed.
    Fetch the manifest directly with the existing p01 token, never the layers.
    """
    if not IMAGE_METADATA_PROXY_URL:
        return ""
    proxy = urllib.parse.urlsplit(IMAGE_METADATA_PROXY_URL)
    token = os.environ.get("K8S_TOKEN", "")
    if (
        proxy.scheme != "https" or not proxy.hostname or proxy.username
        or proxy.password or proxy.path not in ("", "/") or proxy.query
        or proxy.fragment or not token
    ):
        logger.warning("Image metadata requires an HTTPS proxy origin and K8S_TOKEN")
        return ""
    match = re.fullmatch(
        r"quay\.io/(redhat-user-workloads(?:-stage)?)/"
        + re.escape(TENANT_NAMESPACE)
        + r"/([a-z0-9]+(?:[._/-][a-z0-9]+)*)@(sha256:[0-9a-f]{64})",
        image,
    )
    if not match:
        return ""
    organization, repository, digest = match.groups()
    url = (
        f"{IMAGE_METADATA_PROXY_URL.rstrip('/')}/v2/{organization}/"
        f"{TENANT_NAMESPACE}/{repository}/manifests/{digest}"
    )
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.oci.image.manifest.v1+json",
    })
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            logger.warning("Image metadata manifest exceeds size limit")
            return ""
        manifest = json.loads(raw)
        annotations = manifest.get("annotations", {})
        gav = annotations.get("dev.lightwell.primary-gav", "")
        if not isinstance(gav, str) or not gav.strip():
            return ""
        gav = gav.strip()
        # Do not let artifact-controlled text inject Slack markup or newlines.
        if len(gav) > 512 or not re.fullmatch(r"[A-Za-z0-9_.+:-]+", gav):
            return ""
        count = annotations.get("dev.lightwell.gav-count", "1")
        if isinstance(count, str) and count.isdecimal() and len(count) < 10:
            additional = int(count) - 1
            if additional > 0:
                return f"{gav} (+{additional} additional artifacts)"
        return gav
    except Exception as exc:
        # Avoid exception text: an HTTP client error may contain sensitive data.
        logger.warning("Image metadata lookup failed (%s); using existing title", type(exc).__name__)
        return ""
