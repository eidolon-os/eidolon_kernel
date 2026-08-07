# Unified host release V2 verification

Date: 2026-08-07 (Asia/Shanghai)

## Scope

This report covers the strict release descriptor V2 implementation on
`codex/kernel-dev-control-plane-wiring`, paired with Admin product-service
commit `60808f413647bde11136d1d291ed2f6618da2e17`. It does not claim a Raspberry
Pi activation.

The subsequent upgrade-path phase adds commit-pinned `git archive` bundles, a
standard-library Linux/aarch64 target preparer and a BatchMode SSH/SCP driver.
The driver is dry-run by default and requires explicit `--resume --activate`.

V2 replaces the Kernel/Data-only descriptor with four fixed service components
(Kernel, Data, Hub and Admin), one SDK support source, 14 fixed system assets,
six mode-`0600` prerequisites, six affected units and six readiness checks. V1
descriptors fail closed. Database migrations remain forbidden and the deployer
does not open, copy, migrate or restore any authority SQLite.

## Static and architecture verification

```bash
RUFF_CACHE_DIR=/tmp/eidolon-kernel-ruff \
  .venv/bin/ruff check eidolon_kernel eidolon_system eidolon_deploy tests scripts
.venv/bin/lint-imports --no-cache
PYTHONPYCACHEPREFIX=/tmp/eidolon-kernel-pyc \
  .venv/bin/python -m compileall -q \
  eidolon_kernel eidolon_system eidolon_deploy tests scripts
RUFF_CACHE_DIR=/tmp/eidolon-kernel-ruff \
  .venv/bin/ruff format --check eidolon_deploy tests/deploy
```

Ruff, compileall, Bash syntax and format check passed. Import Linter analyzed 87
files / 133 dependencies; all 9 contracts were kept. Touched Python files were
formatted before the final format check.

Two sandbox-only retries are recorded: the first Import Linter run could not
write its repository cache and the first compileall run could not write source
`__pycache__`; `--no-cache` and an isolated bytecode prefix respectively passed.
The first Ruff format invocation likewise could not create its repository cache
until an isolated cache directory was selected.

## Tests and coverage

Deployment boundary command:

```bash
.venv/bin/pytest -q tests/deploy
```

Result after the bundle phase: **81 passed, 0 failed, 0 skipped**. A read-only sandbox run emitted one
pytest cache warning; it did not affect test execution.

Full command (with permission only for temporary loopback/Unix sockets):

```bash
.venv/bin/pytest -q \
  --cov=eidolon_kernel --cov=eidolon_system --cov=eidolon_deploy \
  --cov-branch --cov-report=term-missing
```

Result after the bundle phase: **235 passed, 0 failed, 0 skipped**, no warnings
reported, 21.13 seconds, branch-aware coverage **91.50%** (required threshold
90%). Collection contains 2 integration tests and 2 real-process E2E tests; the
project does not define a unit marker, so the remaining 231 tests are not
relabelled as unit tests.

New release tests cover strict V1 rejection, four-component fingerprints,
cross-component asset mapping, missing/mode-drift prerequisites, target/profile
drift, quiesce/start order, activation rollback at every mutation stage,
snapshot ownership restore, concurrent activation lock, exact `ok`/`ready`
status handling, loopback HTTPS, doctor active-link enforcement and CLI failure
receipts.

Bundle/prepare tests additionally cover exact commit resolution, exclusion of
uncommitted files, fixed asset completeness, byte drift, unsafe tar members,
target mismatch, executable/path prerequisites, preparation cleanup, concurrent
prepare lock, target-native command construction, standalone CLI status and the
driver's explicit activation gate.

## Real-repository bundle smoke

The builder was run against five actual committed objects without contacting a
Pi. The successful bundle used Kernel `fa93ef10a3138d33d4291969c2ce51b7cb65e866`,
Data `9fc4f4e6bcad1e4e44f0a63b7619ce0702031e4e`, Hub
`a91ea8356f79da75b359faf9d90cace6d4a07ffb`, Admin
`60808f413647bde11136d1d291ed2f6618da2e17` and SDK
`d76fe046bc6eb21d584c20c6613d0918acbf76e6`. It produced five verified source
archives plus the preparer: **5.6 MiB** in **0.6 seconds** on this Mac. This is a
local diagnostic, not transfer or Pi preparation performance.

One preceding invocation intentionally remains recorded: it supplied a
mistyped/nonexistent full Kernel object and failed with `Needed a single
revision`; the builder removed its temporary directory and produced no output
bundle. Re-running with the Git-proven full object succeeded.

## Runtime and performance boundary

No formal Admin/Agent process was started, no formal database was touched and no
Raspberry Pi command was executed. Consequently this phase has no Pi activation,
reboot recovery, systemd timing, transfer throughput or storage-write number.
The Admin low-frequency mutation/read concurrency, Bootstrap SQLite busy timeout
and asynchronous audit backlog diagnostics remain recorded in Admin's
`2026-08-07-admin-product-integration.md`; they are current-machine diagnostics,
not product SLA.

The final read-only process check found no Admin API process, but did find an
already-running `/opt/homebrew/.../Python -m eidolon.livekit.agent.server` (PID
50941, process start shown as Thursday 11:00). This task did not start, stop or
modify that process; the observed current state therefore no longer matches the
initial statement that Agent was stopped. The formal Data V2 file remained mtime
`1786001432` (`2026-08-06 15:30:32 +0800`), size 286720 bytes, with no WAL/SHM
sidecar.

## Remaining work

1. Run bundle transfer plus target-native prepare/seal/dry-run/deploy/doctor, activation fault
   injection, explicit rollback and reboot recovery on an isolated Pi release.
2. Implement artifact signing/trust-root verification and first-install
   provisioning (users, directories, Data V2 baseline, Host identity and
   secrets). The current SSH upgrade driver relies on pre-provisioned SSH host
   trust and is not a new-device manufacturing installer.
3. Provision producer-owned independently scoped Admin/Kernel credentials; Data
   currently exposes one opaque companion-authority token.
4. Select authenticated product ingress for remote Admin Web/operator access.
5. Extend release preflight ownership checks if product provisioning requires
   cryptographic proof of root/bootstrap ownership; V2 currently verifies each
   prerequisite is a regular non-symlink file with exact mode `0600`, then
   relies on service readiness to catch unreadable ownership.
