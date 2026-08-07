# Raspberry Pi target release runbook

本文面向 Eidolon 产品镜像/设备的 root 运维。V2 目标固定为 Raspberry Pi/Linux `aarch64`，同一
release 事务覆盖 Data、Hub、Kernel、Admin，以及 Admin 所有的 Bootstrap/Local API 产品进程。

## 1. Target-native preparation

受控 staging 先创建固定目录：

```text
/srv/eidolon/releases/<release_id>/
├── eidolon_kernel/
├── eidolon_data/
├── eidolon_hub/
├── eidolon_admin/
└── eidolon_sdk/
```

五棵 source 必须对应 review 后的完整 Git object ID。四个 service component 必须使用目标 Python
建立 `.venv` 并以 lock 安装；Data 必须包含 `api` extra。SDK 是 Data/Admin 的同 release support
source，不是 service component。activation 不联网、不安装依赖，也不执行 descriptor 提供的命令。

首次装机还必须由 image/provisioning 边界创建 `eidolon`、`eidolon-bootstrap` 用户和目录，生成 Data V2
空库，制造期写入 Host identity，并创建以下 mode `0600` 文件：

```text
/etc/eidolon/data.env
/etc/eidolon/hub.env
/etc/eidolon/kernel.env
/etc/eidolon/admin.env
/etc/eidolon/bootstrap.env
/var/lib/eidolon-bootstrap/host_identity.ed25519
```

正常 release 不创建、迁移、备份或旋转这些权威状态。当前仓库尚未提供 source transfer/first-install
provisioner；不得把下面的 target-native 命令描述成已经实现的无人值守远程安装。

## 2. Seal / prepare

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release seal \
  <release_id> \
  --kernel-revision <40-hex-kernel-commit> \
  --data-revision <40-hex-data-commit> \
  --hub-revision <40-hex-hub-commit> \
  --admin-revision <40-hex-admin-commit> \
  --sdk-revision <40-hex-sdk-commit>
```

成功生成 strict V2 `release.json` 和 `.sha256` sidecar。同一 release 不允许重新 seal；checksum 是本地
完整性证据，不是发布签名。

## 3. Dry-run

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release deploy \
  /srv/eidolon/releases/<release_id>/release.json --dry-run
```

dry-run 持有短暂排他锁并执行完整预检，但不停止服务、不创建 snapshot、不切换 link、不改系统资产。

## 4. Deploy

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release deploy \
  /srv/eidolon/releases/<release_id>/release.json
```

事务先停止 Admin、Local API、Bootstrap，再停止 eidolond 与其 Data/Hub/Kernel children；随后原子安装
14 个 allowlist 资产、切换四个 component symlink、reload systemd，并按 Bootstrap → eidolond →
Local API → Admin 顺序启动。Data/Hub/Kernel 仍由 eidolond desired state 拉起，不形成第二个 lifecycle
authority。

六个就绪检查分别验证 eidolond UDS、Data、Hub、Kernel、Admin loopback HTTP 与 Local API loopback
HTTPS。HTTPS readiness 只证明本机进程与 Bootstrap 可用，不替代移动端的 Host ID/SPKI 校验。

成功 receipt 和 rollback snapshot 位于：

```text
/var/lib/eidolon/deployments/<release_id>-<transaction_id>/snapshot.json
/var/lib/eidolon/deployments/<release_id>-<transaction_id>/receipt.json
```

## 5. Doctor

```bash
sudo /srv/eidolon/current/eidolon_kernel/.venv/bin/eidolon-release doctor \
  /srv/eidolon/releases/<release_id>/release.json
```

doctor 只读复核 descriptor/source/lock/venv/entrypoint/asset/secret、四个 active link、七个 systemd
unit 和六个 readiness。输出 `status=healthy` 才表示这一个 release 的当前主机状态一致。

## 6. Rollback

失败且自动恢复成功时返回非零并输出 `status=rolled_back`；`rollback_failed` 表示主机状态未知，必须停止
自动重试并保留 journal/snapshot。显式恢复命令为：

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release rollback \
  /srv/eidolon/releases/<release_id>/release.json \
  /var/lib/eidolon/deployments/<release_id>-<transaction_id>
```

rollback 只恢复 allowlist 系统资产和四个 symlink，不修改 secret、Host identity 或任何 SQLite。V2 仍
强制 `database_migrations=[]`；首个 schema 变更必须另行定义 authority-owned backup/forward/rollback
语义。
