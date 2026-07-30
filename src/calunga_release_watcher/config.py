import os

TENANT_NAMESPACE = os.environ.get("TENANT_NAMESPACE", "calunga-tenant")
RELEASE_NAMESPACE = os.environ.get("RELEASE_NAMESPACE", "rhtap-releng-tenant")
APPLICATION = os.environ.get("APPLICATION", "calunga-v2-index-main")
APPLICATIONS = {app.strip() for app in APPLICATION.split(",") if app.strip()}

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
STALL_TIMEOUT_MINUTES = int(os.environ.get("STALL_TIMEOUT_MINUTES", "30"))

# Retry mechanism
RETRY_ENABLED = os.environ.get("RETRY_ENABLED", "false").lower() == "true"
RETRY_CONFIDENCE_THRESHOLD = os.environ.get("RETRY_CONFIDENCE_THRESHOLD", "medium")

# AI failure analysis
AI_ANALYSIS_ENABLED = os.environ.get("AI_ANALYSIS_ENABLED", "false").lower() == "true"
GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
GOOGLE_CLOUD_REGION = os.environ.get("GOOGLE_CLOUD_REGION", "global")
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")
AI_MAX_LOG_LINES = int(os.environ.get("AI_MAX_LOG_LINES", "200"))
AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "30"))

# Label keys
LBL_PIPELINE_TYPE = "pipelines.appstudio.openshift.io/type"
LBL_APPLICATION = "appstudio.openshift.io/application"
LBL_COMPONENT = "appstudio.openshift.io/component"
LBL_SNAPSHOT = "appstudio.openshift.io/snapshot"
LBL_BUILD_PLR = "appstudio.openshift.io/build-pipelinerun"
LBL_RELEASE_NAME = "release.appstudio.openshift.io/name"
LBL_RELEASE_NS = "release.appstudio.openshift.io/namespace"
LBL_TEST_EVENT_TYPE = "pac.test.appstudio.openshift.io/event-type"
LBL_BUILD_EVENT_TYPE = "pipelinesascode.tekton.dev/event-type"
LBL_BUILD_SHA = "pipelinesascode.tekton.dev/sha"
ANN_BUILD_SHA_TITLE = "pipelinesascode.tekton.dev/sha-title"

# Annotation keys shared across PAC resources
ANN_TEST_SHA = "pac.test.appstudio.openshift.io/sha"
ANN_TEST_SHA_TITLE = "pac.test.appstudio.openshift.io/sha-title"
ANN_TEST_STATUS = "test.appstudio.openshift.io/status"

# Also present as labels on all resources
LBL_TEST_SHA = "pac.test.appstudio.openshift.io/sha"

# Integration test / retry labels
LBL_SCENARIO = "test.appstudio.openshift.io/scenario"
LBL_ITS_RUN = "test.appstudio.openshift.io/run"

# Release labels
LBL_RELEASE_PLAN = "release.appstudio.openshift.io/releasePlan"
LBL_RELEASE_SNAPSHOT = "release.appstudio.openshift.io/snapshot"
LBL_AUTOMATED = "release.appstudio.openshift.io/automated"
