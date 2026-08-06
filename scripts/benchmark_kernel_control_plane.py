"""Reproducible local diagnostic for the Kernel's low-frequency SQLite writer."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from eidolon_kernel.adapters.persistence.sqlite import SqliteMountStore
from eidolon_kernel.domain.model import DeviceMount, request_fingerprint


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int((len(ordered) - 1) * percentile))
    return ordered[index]


def _mount(index: int) -> DeviceMount:
    request_id = f"benchmark-mount-{index}"
    return DeviceMount.first(
        device_id=f"benchmark-device-{index:08d}",
        owner_id="benchmark-owner",
        at=datetime.now(UTC),
        request_id=request_id,
        fingerprint=request_fingerprint(
            "device.mount",
            {
                "device_id": f"benchmark-device-{index:08d}",
                "owner_id": "benchmark-owner",
                "expected_revision": 0,
                "replace_existing": False,
            },
        ),
    )


def _commit(store: SqliteMountStore, index: int) -> float:
    started = time.perf_counter_ns()
    store.commit(
        mount=_mount(index),
        expected_revision=0,
        operation="device.mount",
        event_type="eidolon.kernel.device-mounted.v1",
        event_data={"benchmark": True},
    )
    return (time.perf_counter_ns() - started) / 1_000_000


def run(*, iterations: int, workers: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="eidolon-kernel-benchmark-") as temp:
        path = Path(temp) / "kernel.sqlite3"
        store = SqliteMountStore(path)
        try:
            sequential = [_commit(store, index) for index in range(iterations)]
            concurrent_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as executor:
                concurrent = list(
                    executor.map(
                        lambda index: _commit(store, index),
                        range(iterations, iterations * 2),
                    )
                )
            concurrent_elapsed = time.perf_counter() - concurrent_started
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                pragmas = {
                    "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                    "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                    "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                    "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
                }
                counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "kernel_device_mounts",
                        "kernel_requests",
                        "kernel_audit_events",
                    )
                }
            finally:
                connection.close()
        finally:
            store.close()
    return {
        "iterations_per_lane": iterations,
        "workers": workers,
        "sequential_commit_ms": {
            "p50": round(median(sequential), 3),
            "p95": round(_percentile(sequential, 0.95), 3),
            "max": round(max(sequential), 3),
        },
        "concurrent_commit_ms": {
            "p50": round(median(concurrent), 3),
            "p95": round(_percentile(concurrent, 0.95), 3),
            "max": round(max(concurrent), 3),
            "throughput_per_second": round(iterations / concurrent_elapsed, 1),
        },
        "pragmas": pragmas,
        "row_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.iterations < 1 or args.workers < 1:
        parser.error("iterations and workers must be positive")
    print(json.dumps(run(iterations=args.iterations, workers=args.workers), indent=2))


if __name__ == "__main__":
    main()
