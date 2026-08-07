# Target release runbook

本文只面向 Eidolon 产品镜像/设备的 root 运维，不是终端用户 CLI。当前目标固定为
Raspberry Pi/Linux `aarch64`，部署事务只切换 Kernel/Data；Hub、Admin、Bootstrap 不在范围内。

## 1. 准备阶段（允许网络，不属于 activation）

在受控 staging 中为唯一 `release_id` 创建：

```text
/srv/eidolon/releases/<release_id>/eidolon_kernel
/srv/eidolon/releases/<release_id>/eidolon_data
/srv/eidolon/releases/<release_id>/eidolon_sdk
```

三棵 source 必须对应 review 后的完整 Git object ID 且无开发期缓存/未跟踪产物。Kernel/Data 使用目标
Python 创建原生 venv；Data 安装 `api` extra 和同 release 根的 SDK。完成测试和 import smoke 后再 seal，
不要先 seal 再修改 source 或 venv。activation 阶段不会联网、安装依赖或修复环境。

当前 V1 不提供源码下载/通用 installer。流水线必须显式完成 source staging 和 native environment build；
这是避免把 Git credential、package registry、任意脚本和跨平台构建混入 root activation 的边界。

## 2. Seal

在目标机使用新 Kernel venv 中的运维入口：

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release seal \
  <release_id> \
  --kernel-revision <40-hex-kernel-commit> \
  --data-revision <40-hex-data-commit> \
  --sdk-revision <40-hex-sdk-commit>
```

成功生成 `release.json` 和 `release.json.sha256`。同一 release 不允许重新 seal。checksum 只提供本地
完整性，不是发布签名。

## 3. Dry-run

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release activate \
  /srv/eidolon/releases/<release_id>/release.json --dry-run
```

dry-run 持有短暂排他锁并执行完整预检，但不停止服务、不创建 snapshot、不切换 link、不改系统资产。
必须保存 JSON 输出，并确认 previous targets 正是当前期望 release。

## 4. Activate

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release activate \
  /srv/eidolon/releases/<release_id>/release.json
```

成功输出 `status=activated` 与 transaction ID。对应证据在：

```text
/var/lib/eidolon/deployments/<release_id>-<transaction_id>/snapshot.json
/var/lib/eidolon/deployments/<release_id>-<transaction_id>/receipt.json
```

当前 snapshot 内部格式为 V2：既有系统资产除备份内容/mode 外还记录原始数值 UID/GID，rollback 在
原子替换前恢复 ownership。缺少这些字段的开发期 V1 snapshot 会在停服务前被拒绝；不要手工补字段、
猜测用户组或跨版本复用 snapshot。

不要删除旧 release 或 snapshot。随后复核 eidolond directory、Data/Kernel health、关键 SQLite
`PRAGMA integrity_check`，再执行整机 reboot/recovery 验证。

## 5. 自动失败与显式 rollback

activation 失败且自动恢复成功时返回非零并输出 `status=rolled_back`；先验证旧服务和数据库，不要立刻
重复执行。若输出 `rollback_failed`，系统状态未知，应停止自动重试，保留 snapshot/journal 并人工处置。

已成功 activation 也可显式恢复：

```bash
sudo /srv/eidolon/releases/<release_id>/eidolon_kernel/.venv/bin/eidolon-release rollback \
  /srv/eidolon/releases/<release_id>/release.json \
  /var/lib/eidolon/deployments/<release_id>-<transaction_id>
```

rollback 只恢复 allowlist 系统资产和 Kernel/Data symlink；不会修改 secret 或任何 SQLite。若新版本已执行
数据库 migration，本命令不具备安全语义——V1 因此直接拒绝带 migration 的 descriptor。
