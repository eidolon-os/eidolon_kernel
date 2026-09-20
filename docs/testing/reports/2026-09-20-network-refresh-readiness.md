# LiveKit restart outcome and Hub authority availability

The Pi5 device inventory returned HTTP 503 after moving from 192.168.1.x to
192.168.100.x. Local Controller authentication and Hub configuration pulls still
returned 200. eidolond hid `hub/device-authority.http` as dependency-blocked.

The first repeated LiveKit restart timeout in this boot is at 22:09:49, before
the Mobile APK update at 22:55. The applier caller waits 20 seconds, while
LiveKit drains participants until systemd's stop timeout and subsequently starts
a new process. The request times out before that result is observed. The manager
only records the network input after a successful synchronous reply, so every
new process remains "unapplied" and receives another restart roughly 30 seconds
later. Merely lengthening a timeout would leave ambiguous outcomes unhandled.

The fix retains the network input and pre-operation process instance for an
attempt. Reconciliation observes completion before deciding to retry: a new
active instance under the same continuously observed network input can be
recorded, then must pass its health probe. An unchanged instance, unavailable
network or changed input cannot satisfy the attempt. Real failures retain the
existing retry backoff. No durable authorization or generation state changes.

A separate dependency error amplified the media failure: the entire Hub
identity/Claim directory depended on LiveKit and channel-provider readiness.
Hub's registry is independently healthy and operation-specific channel calls
already handle provider failures. The product manifest now exposes the device
authority based on Hub health, keeping media dependencies on the media services.
This uses the existing manifest semantics; it adds no new readiness framework.

Validation: `pytest -q tests/system` passes 136 tests with one skip; Ruff passes
for changed Python files. Regression cases cover a restart completing after a
lost/timeout response without another restart, rejecting completion on a changed
network, real failures/backoff, and device authority availability while LiveKit
is unavailable. Live deployment results are recorded separately in Ops.
