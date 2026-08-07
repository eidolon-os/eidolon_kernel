# Unified host release V2 verification

Date: 2026-08-07 (Asia/Shanghai)

## Scope

This report covers the strict release descriptor V2 implementation on
`codex/kernel-dev-control-plane-wiring`, paired with Admin product-service
commit `60808f413647bde11136d1d291ed2f6618da2e17`. It does not claim a Raspberry
Pi activation.

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

Ruff, compileall and format check passed. Import Linter analyzed 85 files / 130
dependencies; all 9 contracts were kept. Ruff formatted 8 touched files before
the final format check.

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

Result: **69 passed, 0 failed, 0 skipped**. A read-only sandbox run emitted one
pytest cache warning; it did not affect test execution.

Full command (with permission only for temporary loopback/Unix sockets):

```bash
.venv/bin/pytest -q \
  --cov=eidolon_kernel --cov=eidolon_system --cov=eidolon_deploy \
  --cov-branch --cov-report=term-missing
```

Result: **223 passed, 0 failed, 0 skipped**, no warnings reported, 16.10 seconds,
branch-aware coverage **92.28%** (required threshold 90%). Collection contains
2 integration tests and 2 real-process E2E tests; the project does not define a
unit marker, so the remaining 219 tests are not relabelled as unit tests.

New release tests cover strict V1 rejection, four-component fingerprints,
cross-component asset mapping, missing/mode-drift prerequisites, target/profile
drift, quiesce/start order, activation rollback at every mutation stage,
snapshot ownership restore, concurrent activation lock, exact `ok`/`ready`
status handling, loopback HTTPS, doctor active-link enforcement and CLI failure
receipts.

## Runtime and performance boundary

No formal Admin/Agent process was started, no formal database was touched and no
Raspberry Pi command was executed. Consequently this phase has no Pi activation,
reboot recovery, systemd timing, transfer throughput or storage-write number.
The Admin low-frequency mutation/read concurrency, Bootstrap SQLite busy timeout
and asynchronous audit backlog diagnostics remain recorded in Admin's
`2026-08-07-admin-product-integration.md`; they are current-machine diagnostics,
not product SLA.

## Remaining work

1. Run target-native prepare/seal/dry-run/deploy/doctor, activation fault
   injection, explicit rollback and reboot recovery on an isolated Pi release.
2. Implement trusted artifact transport and first-install provisioning (users,
   directories, Data V2 baseline, Host identity and secrets). Until then the
   tool is an offline prepared-release activator, not a workstation-to-Pi
   one-command installer.
3. Provision producer-owned independently scoped Admin/Kernel credentials; Data
   currently exposes one opaque companion-authority token.
4. Select authenticated product ingress for remote Admin Web/operator access.
