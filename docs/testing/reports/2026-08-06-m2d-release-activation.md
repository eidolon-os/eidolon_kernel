# M2-D Prepared Target Release 测试报告

- 日期：2026-08-06 至 2026-08-07
- 范围：release wire contract、target sealing、Linux preflight、原子激活、自动/显式回滚、整机重启恢复
- 当前状态：本地实现、真机缺陷修复与 Raspberry Pi 5 验证完成

## 已验证边界

- `eidolon_deploy` 与 Kernel/eidolond runtime package 整包 independence；runtime 不 import 部署实现；
- descriptor 只接受 Kernel/Data service component 和一个真实 Data SDK support source；
- target、release/current path、system asset、secret、unit、readiness 与空 migration 均是固定 allowlist；
- source/lock/installed distribution/asset checksum、Python version、entrypoint、secret mode 与 systemd unit
  在停服务前一次性检查；
- 排他 host lock 阻止并发 activation；dry-run 不创建 snapshot、不停服务、不切换 link 或系统资产；
- snapshot、资产安装、symlink 替换、receipt 均使用本地原子替换；
- quiesce 后任一步失败都会进入 restore；restore 失败单独上报，不能伪造成功；
- snapshot V2 保存既有系统资产的数值 UID/GID；缺失 ownership 的 V1 snapshot 在停服务前 fail closed；
- secret 和 SQLite 从不进入 release snapshot，descriptor V1 对非空 migration 直接拒绝。

## 本地结果

ownership 修复的测试驱动证据：新增测试先在 snapshot V1 上失败，随后验证 V2 记录/恢复 UID/GID，
并验证缺失 ownership 的 snapshot 在执行任何 `systemctl stop` 前被拒绝。

```text
deployment tests: 65 passed
shared Eidolon workspace: 219 passed, 0 skipped
independent Kernel clone: 215 passed, 4 skipped（缺少 Data/Hub sibling checkout/runtime）
combined branch coverage: 92.21%（门槛 90%）
Ruff: passed
Import Linter: 9 contracts kept, 0 broken
uv lock --check --no-cache: passed
```

共享 workspace 的全量进程 E2E 在允许本机 loopback/Unix socket 的执行上下文运行，没有使用 skip 或
降低覆盖率门槛。独立 clone 的四个 skip 均由测试自身明确报告为 sibling development runtime/checkout
不存在；同一四项已在共享 workspace 通过。独立 clone 同时证明 `.git` 为真实目录时的仓库独立性门禁。

## Raspberry Pi 5 结果

目标事实：

```text
host: eidolon-pi5@192.168.1.26
system: Debian 13 / systemd 257 / linux aarch64 / Python 3.13.5
final release: 20260807-m2d-530d74e-r2
Kernel revision: 530d74ee378f4a40bd867c1689d806e7eb8bc400
Data revision: 2a338940681fb281e1d962f891f2d675a0bb97b5
SDK revision: d76fe046bc6eb21d584c20c6613d0918acbf76e6
final transaction: 1af8593598c045c7be07fc48662fb5ba
final boot id: 5cfc7922-1589-4971-af59-8c5604259f02
```

Kernel/Data 在目标机以两个隔离 venv 离线原生重建；`pip check` 分别验证 29/49 个 distribution，三个
本地 project wheel、imports、固定 entrypoint 与绝对 shebang 均通过。descriptor SHA-256 与 sidecar
一致，dry-run 返回正确 previous targets 且没有产生 deployment mutation。

### 真机发现与修复

首个 V1 候选成功激活后，跨进程显式 rollback 暴露真实缺陷：`shutil.copy2` 保留 mode/mtime 但不保留
owner/group，原先的 `/etc/eidolon/eidolond.yaml` 从 `0640 root:eidolon` 被恢复为 `0640 root:root`，旧
eidolond 因 `PermissionError` 无法读取配置。现场只恢复该文件原有 ownership 并重启 eidolond，M2-C
随即恢复；没有改数据库、Hub/Admin/Bootstrap release 或 secret。

修复 commit `530d74e` 将 snapshot 提升为 V2，记录 UID/GID 并在原子替换前 `chown` 临时文件。V2
真机 snapshot 明确记录 `eidolond.yaml uid=0/gid=983`。显式 rollback 后文件精确恢复为
`0640 0:983 root:eidolon`，receipt 为 `restored`，旧 eidolond/Data/Kernel 全部 ready，日志无 warning。

故障注入使用 `RuntimeMaxSec=50s` 的瞬态 systemd unit 竞争本地 `127.0.0.1:8083`，没有修改持久配置。
activation 在 `readiness timeout: kernel` 后自动恢复，事务 `072774593dc844dc8e6601e3f884d8a8` 的 receipt
为 `rolled_back`；瞬态 unit 消失后旧 Kernel 恢复，ownership 与旧 link 均正确。

一次中间候选的 source/environment hash 正确，但人工提供的完整 Kernel revision label 未经
`git rev-parse` 核对。该候选没有被冒充为最终证据；另建唯一 r2 release，用上方三个真实完整 Git
object ID 重新原生构建、seal、dry-run、activate 和 reboot。

### 最终恢复证据

- `/srv/eidolon/current/eidolon_kernel` 与 `eidolon_data` 均指向 r2 release；
- Bootstrap、Hub、Data、Kernel、eidolond 均 `active/running`，本 boot 的 `NRestarts=0`；
- eidolond UDS、Data、Hub、Kernel health 分别为 ready/ready/ok/ready，本 boot 无 warning；
- eidolond、Data、Hub、Kernel 四个 SQLite 均以只读 URI 执行 `PRAGMA integrity_check=ok`；
- Kernel 保持 1 条 active Mount 与 2 条 audit event；owner-scoped Resolve/List 仍返回
  `device-pi-m2b-20260806 → companion-pi-m2c-20260806`、revision 2；
- eidolond 从 manifest/SQLite 重建 Hub device authority 与 Data companion authority endpoint 投影；
- Hub PID 在 release activation 期间不变；最终重启也没有切换或修改 Hub/Admin/Bootstrap release。

## 剩余风险

- checksum 只证明 root-owned 本地 staging 完整性，不是签名或来源认证；
- V1 没有 artifact registry、OTA server、TPM trust root、A/B 分区或断电事务证明；
- snapshot V2 只服务同机 rollback，不迁移数值 UID/GID，也不覆盖 ACL/xattr；当前 allowlist 资产不依赖
  ACL/xattr，出现真实需求后必须先定义并测试契约；
- 首个数据库 migration 仍是独立门槛，不能放入当前 symlink rollback 语义。
