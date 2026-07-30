# Plan: Fix state machine to prevent stale-event retry corruption

## Context

When kopf's watch stream reconnects (every ~270s), it replays all recent events. The current state machine has two bugs that interact to break retry chains:

**Bug 1 — backward state rewinding**: Guard 3 in `_transition()` only blocks `RETRYING → FAILURE`. It does NOT block `RETRYING → BUILD_SUCCEEDED` or `RETRYING → TESTING`. So stale replayed BUILD_SUCCEEDED / TESTING events can rewind state from `RELEASE_RETRYING` back to an earlier stage.

**Bug 2 — guard 3 also blocks legitimate retry chains**: Once state is rewound to `TESTING` (via stale event), the next stale managed-PLR failure event triggers `_handle_failure` again (working around guard 3 by accident). After retry 2 is created and `RELEASE_RETRYING` is set, no more state rewinds occur — so the retry 2 failure IS blocked by guard 3. Result: retry chain silently stops with no "max retries exhausted" message.

Observed sequence:
```
02:09:32 — managed-8svvr fails → RELEASE_FAILED → retry 1 → RELEASE_RETRYING
02:10:13 — stale BUILD_SUCCEEDED (624rf) replayed → RELEASE_RETRYING → BUILD_SUCCEEDED  (bug 1)
02:10:25 — stale TESTING replayed → BUILD_SUCCEEDED → TESTING
02:14:33 — stale managed-8svvr replayed → TESTING → RELEASE_FAILED → retry 2 → RELEASE_RETRYING
           (retry 2 fails with same "automated" label bug — now fixed)
           retry 2 failure blocked by guard 3 → no retry 3, no max-retries message  (bug 2)
```

## Implementation

All changes are in **`src/calunga_release_watcher/tracker.py`** only.

### Step 1 — Add `processed_failure_sources` to PipelineInfo

```python
@dataclass
class PipelineInfo:
    ...
    processed_failure_sources: set[str] = field(default_factory=set)
```

This set tracks every PLR/release name that has already triggered a failure transition. Used to deduplicate stale replayed failure events.

### Step 2 — Replace guard 3 with a backward-blocking guard

Remove the current guard 3:
```python
# REMOVE:
if old_state in RETRYING_STATES and new_state in FAILURE_STATES:
    return
```

Add a module-level dict and a new guard that blocks backward state rewinds from RETRYING states, but **allows** `RETRYING → FAILURE` (so retry chains work):

```python
# Module level (near RETRYING_STATES definition):
_RETRYING_BLOCKED_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.RELEASE_RETRYING: frozenset({
        PipelineState.BUILD_RUNNING,
        PipelineState.BUILD_SUCCEEDED,
        PipelineState.SNAPSHOT_CREATED,
        PipelineState.TESTING,
        PipelineState.TESTS_PASSED,
        PipelineState.RELEASING,
    }),
    PipelineState.TESTS_RETRYING: frozenset({
        PipelineState.BUILD_RUNNING,
        PipelineState.BUILD_SUCCEEDED,
        PipelineState.SNAPSHOT_CREATED,
    }),
    # BUILD_RETRYING: nothing to block (it's the first stage)
}
```

In `_transition()`, replace guard 3 with:
```python
if new_state in _RETRYING_BLOCKED_TRANSITIONS.get(old_state, frozenset()):
    return
```

Effect:
- `RELEASE_RETRYING → BUILD_SUCCEEDED`: **blocked** (prevents stale rewind)
- `RELEASE_RETRYING → TESTING`: **blocked** (prevents stale rewind)
- `RELEASE_RETRYING → RELEASE_FAILED`: **allowed** (retry chain proceeds)
- `RELEASE_RETRYING → RELEASED`: **allowed** (retry succeeded)
- `TESTS_RETRYING → TESTS_FAILED`: **allowed** (retry chain)
- `TESTS_RETRYING → TESTING`: **allowed** (tests re-running after retry)
- `TESTS_RETRYING → BUILD_SUCCEEDED`: **blocked** (stale rewind)

### Step 3 — Add source dedup before each failure transition

Since guard 3 is removed, stale failure events (same PLR/release) can now reach `_transition` and trigger duplicate `_handle_failure` calls. Deduplicate in each handler before calling `_transition` for a failure state:

**In `on_release_pipelinerun`** (status == "False" branch):
```python
source_key = f"plr:{namespace}/{name}"
if source_key in info.processed_failure_sources:
    return
info.processed_failure_sources.add(source_key)
# then call _transition(info, RELEASE_FAILED, ...)
```

**In `on_release`** (released_status == "False" branch):
```python
source_key = f"release:{name}"
if source_key in info.processed_failure_sources:
    return
info.processed_failure_sources.add(source_key)
# then call _transition(info, RELEASE_FAILED, ...)
```

**In `on_build_pipelinerun`** (status == "False" branch):
```python
source_key = f"build:{name}"
if source_key in info.processed_failure_sources:
    return
info.processed_failure_sources.add(source_key)
```

**In `on_snapshot`** (test_status == "False" branch):
```python
source_key = f"snapshot:{name}"
if source_key in info.processed_failure_sources:
    return
info.processed_failure_sources.add(source_key)
```

### Result after both fixes

```
managed-8svvr fails → RELEASE_FAILED → retry 1 → RELEASE_RETRYING
stale BUILD_SUCCEEDED (624rf) → RELEASE_RETRYING → BLOCKED (new backward guard)  ✓
stale managed-8svvr → source_key already in processed_failure_sources → return    ✓
retry 1's new managed PLR fails → new source_key → RELEASE_RETRYING → RELEASE_FAILED → retry 2
retry 2's new managed PLR fails → new source_key → RELEASE_RETRYING → RELEASE_FAILED → retry 3
retry 3 fails → release_retry_count=3, 3 < 3 false → "max retries exhausted" message  ✓
```

## Files changed

- `src/calunga_release_watcher/tracker.py` only — no other files touched

## Verification

1. Deploy new image, trigger a release failure that gets classified as fluke
2. Watch logs: should see attempt 1/N, then attempt 2/N, then attempt 3/N, then either success or "max retries exhausted"
3. No spurious retries triggered by stale BUILD_SUCCEEDED events between retries
4. Slack thread should have the full chain: ❌ failure → 🔄 retry 1 → 🔄 retry 2 → 🔄 retry 3 → ⚠️ max retries OR ✅ released
