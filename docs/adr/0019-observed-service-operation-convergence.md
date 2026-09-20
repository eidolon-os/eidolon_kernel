# ADR 0019: Reconcile service operations from durable intent and host observations

Status: Implemented. Supersedes the network-specific attempt bookkeeping in the
2026-09-20 readiness fix and the synchronous completion assumption in ADR 0017.

## Problem

A systemctl caller can time out while systemd is still draining a process. The
old reconciler interpreted a missing reply as an unperformed restart. Even the
first repair remembered only network refreshes in RAM: daemon replacement lost
that memory, manual restart persisted its receipt after execution, and disable
published inactive before observing a completed stop.

These are one problem: confusing command transport, operation execution, and
service readiness. Extending timeouts or adding a handler for each caller does
not establish a reliable ordering.

## Responsibilities

- `ServiceManager` decides desired state, dependency gates, network policy and
  endpoint readiness. It does not own process jobs.
- `RuntimeCoordinator` uses one rule for start, stop and restart: persist intent,
  submit when eligible, then observe completion. Manual and automatic requests
  use the same path. No separate network retry loop remains.
- Host adapters expose active state, process instance identity and whether a job
  is queued/running. systemd and supervisord own execution and their deadlines.
- SQLite owns durable intent and request idempotency. Network receipts remain a
  disposable observation cache. Neither is Device/Owner authority.

systemd mutation uses `systemctl --no-block`: successful return means verified
and enqueued, not completed. Inspection includes `Job`, `ActiveState`, `SubState`
and `InvocationID`; queued jobs block resubmission even while the old process is
still active. supervisord STARTING, STOPPING and BACKOFF are also transitions.

Sources: [systemctl --no-block](https://github.com/systemd/systemd/blob/main/man/systemctl.xml),
[systemd unit/job model](https://wiki.freedesktop.org/www/Software/systemd/dbus/).

## Invariants

1. No host mutation before durable intent. A manual restart's revision check,
   receipt, audit and intent commit together. Replaying its request ID cannot
   create another intent. A different restart request conflicts while one is
   pending instead of overtaking it.
2. At most one unfinished intent per service, enforced by a SQLite unique index.
   A host transition always takes precedence over any retry deadline.
3. Completion is an observation: start requires active, stop requires inactive,
   and restart requires active with a different observable process instance.
   All require the host to be out of transition. A reply alone satisfies none.
4. After restart of eidolond, inspect the surviving job/instance first. If its
   outcome is still unknown and no job is visible, allow a 30-second grace period
   before retry. A completed replacement is adopted without another command.
5. Once the current host job settles, the latest enabled/disabled state wins.
   Opposite commands are never submitted concurrently by this coordinator.
6. Network observation records the input submitted and the resulting instance.
   Old-network completion cannot acknowledge a new network. A retry captures
   the current settled input before dispatch. Persist observation before
   removing intent so a failed cache/receipt write cannot cause another restart.
7. Endpoints are withheld during pending operations. Health probes must pass;
   the instance and network are checked again after awaiting probes. A successful
   probe for an instance replaced during that await cannot publish readiness.
8. Identity/Claim reads do not depend on media readiness. Hub remains governed
   by its own health; media calls encounter media availability at their boundary.

## Persistence and compatibility

Use the existing request/audit ledger, with private `runtime_intent` metadata in
`outcome_json`. Existing public desired-state fields and receipt replay stay
unchanged. Completing an intent removes only that metadata. Automatic operations
now have `system.runtime.start/stop/restart` audit entries; retries reuse the same
intent. The audit API already permits these operation strings.

A partial unique index includes unfinished receipts only, keyed by service ID.
The table schema and version remain v1, and old readers ignore extra JSON fields.
No authority DB migration or second durable store is introduced. Rolling back to
old code remains readable, but naturally loses the new coordination guarantees
while that older code is running. Pending metadata is retained for re-upgrade.

## Verification and limits

Tests exercise accepted and lost replies for all three verbs, jobs lasting well
past the retry window, daemon/store reopen, crashes before submission and after
completion, stale revisions, storage failure, same-instance false completion,
opposite desired-state changes, network changes during a pending job or health
probe, and observation persistence failure. Adapter tests cover queued jobs
with an old active instance, inactive states, and supervisord transitions.

This is convergence, not an exactly-once transaction with an external process
manager. An unrelated operator can replace a process between observations, and
host/IPC failures can leave outcomes uncertain. The coordinator uses observable
facts and bounded retries, never promises execution history it cannot prove.
A permanently stuck host job remains pending rather than being overlapped; the
host manager's own timeout/failure policy must settle it. Readiness is sampled,
not a lease preventing a process from dying immediately after publication.

Physical Wi-Fi relocation and end-to-end audio remain separate product acceptance
cases. These tests do not claim preservation of an in-flight conversation across
a network move.
