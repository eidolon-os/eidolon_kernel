# Raspberry Pi target release runbook

本文面向 Eidolon 产品镜像/设备的 root 运维。V2 目标固定为 Raspberry Pi/Linux `aarch64`，同一
release 事务覆盖 Data、Hub、Kernel、Admin，以及 Admin 所有的 Bootstrap/Local API 产品进程。

## 1. Commit-pinned bundle（工作站）

五个 revision 都必须显式给出完整 40-hex commit ID；工具使用 `git archive <commit>`，不会读取或夹带
working-tree 修改。输出是一个包含五个 source tar、strict manifest、逐文件 SHA-256 和 standalone
target preparer 的目录：

```bash
.venv/bin/eidolon-release bundle <release_id> /absolute/path/to/bundle \
  --kernel-repo /path/to/eidolon_kernel \
  --data-repo /path/to/eidolon_data \
  --hub-repo /path/to/eidolon_hub \
  --admin-repo /path/to/eidolon_admin \
  --sdk-repo /path/to/eidolon_sdk \
  --kernel-revision <40-hex-kernel-commit> \
  --data-revision <40-hex-data-commit> \
  --hub-revision <40-hex-hub-commit> \
  --admin-revision <40-hex-admin-commit> \
  --sdk-revision <40-hex-sdk-commit>
```

SHA-256 检测传输/磁盘损坏，不提供来源认证；发布签名和设备 trust root 仍未实现。

## 2. Target-native prepare + seal

将 bundle 传到目标后，以 root 运行其中的纯标准库 preparer：

```bash
sudo python3 /path/to/bundle/prepare_target.py /path/to/bundle \
  --uv /usr/local/bin/uv
```

它校验 bundle、拒绝 unsafe tar member，在 preparation lock 下创建固定目录：

```text
/srv/eidolon/releases/<release_id>/
├── eidolon_kernel/
├── eidolon_data/
├── eidolon_hub/
├── eidolon_admin/
└── eidolon_sdk/
```

随后在目标用 `uv sync --frozen --no-dev` 建立四个原生 `.venv`（Data 明确安装 `api` extra），并调用
新 Kernel venv 的 `eidolon-release seal`。任一受控失败会删除这次新建的 release 目录；既有 release、
current links、secret 和数据库不变。SDK 是 Data/Admin 的 support source，不是 service component。

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

正常 release 不创建、迁移、备份或旋转这些权威状态。当前工具是已 provision 主机的升级路径；没有
实现新机 first-install，不得用它代替制造流程。

Bootstrap 是当前唯一会在应用初始化时演进本地 schema 的 authority。为保证本发布事务仍可代码回滚，
preflight 会分别读取当前 Admin、候选 Admin 声明的 Bootstrap schema 版本以及 SQLite `user_version`，
三者必须完全一致；任何 schema transition 都会在停止服务前 fail closed。制造或独立 migration 流程必须
先定义数据备份、前向迁移和失败恢复，再把新 schema 作为下一次代码发布的共同基线。

## 3. 工作站到 Pi 的统一升级入口

```bash
./deploy/raspberry-pi/eidolon-pi-release.sh \
  --target <user@pi-host> --release-id <release_id> \
  --output /absolute/path/to/bundle \
  --kernel-revision <40-hex-kernel-commit> \
  --data-revision <40-hex-data-commit> \
  --hub-revision <40-hex-hub-commit> \
  --admin-revision <40-hex-admin-commit> \
  --sdk-revision <40-hex-sdk-commit>
```

默认只执行 bundle → BatchMode SSH/SCP transfer → target prepare/seal → deploy dry-run，不切换服务。
复核 JSON previous targets 后，用完全相同参数追加 `--resume --activate`，脚本会重新 dry-run、执行事务并
doctor。SSH host key、账号、sudo policy 和网络连通性由设备运维边界预先配置；脚本不接受密码或内嵌
credential。我们没有在本次实现中连接真实 Pi。

## 4. 独立 Dry-run

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release deploy \
  /srv/eidolon/releases/<release_id>/release.json --dry-run
```

dry-run 持有短暂排他锁并执行完整预检，但不停止服务、不创建 snapshot、不切换 link、不改系统资产。

## 5. 独立 Deploy

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

## 6. Doctor

```bash
sudo /srv/eidolon/current/eidolon_kernel/.venv/bin/eidolon-release doctor \
  /srv/eidolon/releases/<release_id>/release.json
```

doctor 只读复核 descriptor/source/lock/venv/entrypoint/asset/secret、四个 active link、七个 systemd
unit 和六个 readiness。输出 `status=healthy` 才表示这一个 release 的当前主机状态一致。

## 7. Rollback

失败且自动恢复成功时返回非零并输出 `status=rolled_back`；`rollback_failed` 表示主机状态未知，必须停止
自动重试并保留 journal/snapshot。显式恢复命令为：

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release rollback \
  /srv/eidolon/releases/<release_id>/release.json \
  /var/lib/eidolon/deployments/<release_id>-<transaction_id>
```

rollback 只恢复 allowlist 系统资产和四个 symlink，不修改 secret、Host identity 或任何 SQLite。V2 仍
强制 `database_migrations=[]`；Bootstrap 的同版本门禁确保应用启动不会借发布事务隐式跨 schema。
任何新 schema 变更必须另行定义 authority-owned backup/forward/rollback 语义。
