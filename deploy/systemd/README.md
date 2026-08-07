# Raspberry Pi / Linux systemd deployment

These files are product-image inputs, not a source downloader or generic installer. They encode the first
single-host boot boundary:

```text
eidolon-bootstrapd (Admin-owned, always-on onboarding)
        |
        v
eidolond (systemd starts this one unit)
        |
        +-- systemctl start/stop/restart eidolon-data.service
        +-- systemctl start/stop/restart eidolon-data-workspace.service
        +-- systemctl start/stop/restart eidolon-hub.service
        +-- systemctl start/stop/restart eidolon-kernel.service
```

Only `eidolond.service` has an `[Install]` target. Data read authority, Data
Workspace write authority, Hub and Kernel
deliberately have no `WantedBy=` entry: systemd owns their PIDs, cgroups,
signals and crash restart, while `eidolond.sqlite3` remains the sole
desired-state authority. Enabling a child unit independently would create a
second desired-state source.

The image builder must:

1. create the non-login `eidolon` user and group;
2. install these units under `/etc/systemd/system/`;
3. install `../polkit/60-eidolon-system-manager.rules` under
   `/etc/polkit-1/rules.d/`;
4. install reviewed copies of the systemd example YAML files under
   `/etc/eidolon/`; the unit-facing files are named `eidolond.yaml`,
   `kernel.yaml`, and `hub.yaml`, while
   `system-services.systemd.example.yaml` retains its name because
   `eidolond.yaml` resolves that manifest relative to its own directory;
5. install a fresh Eidolon Data V2 database with the Data release's tracked
   Alembic baseline before enabling eidolond; runtime startup validates the
   schema and never creates or repairs it;
6. create root-owned `data.env`, `hub.env`, `kernel.env`, `admin.env`, and
   `local-api.env` files with mode
   `0600` for service credentials; never place secrets in unit files or YAML.
   Data requires `EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN` and the independently
   scoped `EIDOLON_DATA_WORKSPACE_AUTHORITY_TOKEN`; Admin receives the latter as
   `EIDOLON_ADMIN_DATA_WORKSPACE_AUTHORITY_TOKEN`. Local API and Admin share only
   their loopback service credential through `local-api.env` and `admin.env`.
   Hub requires
   `EIDOLON_HUB_MANAGEMENT_JWT_SECRET`,
   `EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN`, and
   `EIDOLON_HUB_CHANNEL_PROVIDER_TOKEN`; Kernel receives the same reader token
   as `EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN` plus the independently scoped
   `EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN`;
7. install Data, Hub and Kernel releases under `/srv/eidolon/current/` with the
   paths used by the units; Data's Python environment must contain its declared
   `api` extra and the reviewed `eidolon-sdk` build it depends on;
8. run `systemd-analyze verify` on all five system-control units, reload systemd, and enable
   only `eidolond.service` (Bootstrap is installed and enabled by its own
   product-image boundary).

The Polkit rule does not grant general systemd administration. It accepts only
requests made by the `eidolon` process running inside `eidolond.service` with
`NoNewPrivileges=yes`, for the exact Data/Data Workspace/Hub/Kernel unit names and the three verbs
implemented by the Host adapter. Unit-file enable/disable and daemon reload are
not granted.

The Hub unit additionally allows `AF_NETLINK`: Linux interface discovery used
by Hub mDNS needs netlink sockets. The remaining address-family restriction is
kept, and Kernel/eidolond do not receive this allowance.

Mobile, Local API and Web Admin do not connect to this socket directly. The
Mobile/Bootstrap completion state is host onboarding state; Kernel/Hub readiness
is a separate application-stack state exposed later through the authenticated
product ingress.

Prepared Eidolon OS V2 target releases are sealed and activated by the independent
root-operator boundary documented in [`docs/operations/target-release.md`](../../docs/operations/target-release.md).
That transaction covers Data, Hub, Kernel, Admin, Bootstrap and Local API assets while
preserving eidolond as the only Data/Hub/Kernel lifecycle authority. It installs only a
fixed allowlist, never copies secrets or databases, and leaves source transfer, first-install
provisioning and native environment construction in the product-image pipeline.
