# Full-product Raspberry Pi runtime verification

- Date: 2026-08-07 (Asia/Shanghai)
- Environment: macOS isolated filesystem, fake command runners, loopback/Unix socket probes
- Real Pi or formal service mutation: none

## Result

```text
uv run ruff check .
All checks passed

uv run pytest --cov=eidolon_deploy --cov-report=term --cov-fail-under=90 -q
247 passed, 0 failed, 0 skipped
eidolon_deploy branch-aware coverage: 90.32%
```

Coverage includes strict 8-source manifests, 7-component sealing, 22 system assets, 11 private prerequisites,
13 affected units, 12 readiness checks, TCP/systemd/generic-2xx probe behavior, exact-commit Channel LFS hydration
and pointer/digest rejection,
snapshot restore, failure rollback, explicit rollback and concurrent activation locks.

`systemd-analyze verify` is exercised through the fixed host command contract and fake failure injection. It was
not executed against these unit files on Linux in this task because the workstation is macOS. Target-native
`uv sync`, real NATS/LiveKit binaries, BlueZ/NetworkManager/Avahi, reboot and phone commissioning remain hardware
acceptance items.
