import logging
import os
import threading
import time

import kopf

from calunga_release_watcher.config import (
    APPLICATION,
    LBL_APPLICATION,
    LBL_BUILD_EVENT_TYPE,
    LBL_TEST_EVENT_TYPE,
    LBL_PIPELINE_TYPE,
    LBL_RELEASE_NS,
    RELEASE_NAMESPACE,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL,
    TENANT_NAMESPACE,
)
from calunga_release_watcher.tracker import PipelineTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("kopf.objects").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

tracker = PipelineTracker()

SYNC_GRACE_PERIOD = 15


def _in_namespace(ns: str):
    return lambda namespace, **_: namespace == ns


def _delayed_set_live():
    time.sleep(SYNC_GRACE_PERIOD)
    tracker.set_live()


# ---------------------------------------------------------------------------
# Build PipelineRuns
# ---------------------------------------------------------------------------
BUILD_FILTER = {LBL_PIPELINE_TYPE: "build", LBL_APPLICATION: APPLICATION, LBL_BUILD_EVENT_TYPE: "push"}


@kopf.on.event("tekton.dev", "v1", "pipelineruns", labels=BUILD_FILTER, when=_in_namespace(TENANT_NAMESPACE))
def on_build_pipelinerun(body, **_):
    tracker.on_build_pipelinerun(body)


# ---------------------------------------------------------------------------
# Test PipelineRuns
# ---------------------------------------------------------------------------
TEST_FILTER = {LBL_PIPELINE_TYPE: "test", LBL_APPLICATION: APPLICATION, LBL_TEST_EVENT_TYPE: "push"}


@kopf.on.event("tekton.dev", "v1", "pipelineruns", labels=TEST_FILTER, when=_in_namespace(TENANT_NAMESPACE))
def on_test_pipelinerun(body, **_):
    tracker.on_test_pipelinerun(body)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
SNAPSHOT_FILTER = {LBL_APPLICATION: APPLICATION, LBL_TEST_EVENT_TYPE: "push"}


@kopf.on.event("appstudio.redhat.com", "v1alpha1", "snapshots", labels=SNAPSHOT_FILTER, when=_in_namespace(TENANT_NAMESPACE))
def on_snapshot(body, **_):
    tracker.on_snapshot(body)


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------
RELEASE_FILTER = {LBL_APPLICATION: APPLICATION, LBL_TEST_EVENT_TYPE: "push"}


@kopf.on.event("appstudio.redhat.com", "v1alpha1", "releases", labels=RELEASE_FILTER, when=_in_namespace(TENANT_NAMESPACE))
def on_release(body, **_):
    tracker.on_release(body)


# ---------------------------------------------------------------------------
# Release (managed) PipelineRuns — different namespace
# ---------------------------------------------------------------------------
MANAGED_FILTER = {LBL_PIPELINE_TYPE: "managed", LBL_RELEASE_NS: TENANT_NAMESPACE}


@kopf.on.event("tekton.dev", "v1", "pipelineruns", labels=MANAGED_FILTER, when=_in_namespace(RELEASE_NAMESPACE))
def on_release_pipelinerun(body, **_):
    tracker.on_release_pipelinerun(body)


# ---------------------------------------------------------------------------
# Operator startup config — watch both namespaces
# ---------------------------------------------------------------------------


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN is required but not set")
    if not SLACK_CHANNEL:
        raise RuntimeError("SLACK_CHANNEL is required but not set")
    settings.watching.server_timeout = 270
    settings.persistence.finalizer = ""
    settings.scanning.disabled = True
    settings.posting.enabled = False
    settings.peering.standalone = True
    settings.networking.trust_env = True
    logger.info(
        "Starting controller — resume sync grace period: %ds. "
        "Events during this window will be processed silently.",
        SYNC_GRACE_PERIOD,
    )
    threading.Thread(target=_delayed_set_live, daemon=True).start()


@kopf.on.login()
def login(**_):
    token = os.environ.get("K8S_TOKEN", "")
    server = os.environ.get("K8S_API_URL", "")
    if token and server:
        logger.info("Using remote cluster: %s", server)
        return kopf.ConnectionInfo(
            server=server,
            token=token,
            ca_path=None,
            insecure=True,
            trust_env=True,
            priority=0,
        )
    return kopf.login_via_client(**_)
