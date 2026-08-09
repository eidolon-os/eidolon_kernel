# ADR-0012: 已准备 Target Release 的离线原子激活

- 状态：Accepted；本地实现、Raspberry Pi 激活/回滚/重启验证完成
- 日期：2026-08-06

## Context

M2-B/M2-C 已在 Raspberry Pi 5 上证明 `systemd → eidolond → Data/Hub/Kernel` 的启动、故障降级和
重启恢复边界，但 release 安装仍由人工按“备份、复制、校验、symlink、restart”执行。继续复制这套
步骤会产生不可审计的顺序差异，也无法证明失败发生在部分资产或部分 symlink 切换后能够回到原状态。

实际部署事实是：

- systemd 只负责启动 `eidolond`；Data/Hub/Kernel desired state 仍属于 eidolond SQLite；
- Kernel 与 Data 由当时的 `/srv/eidolon/current/*` symlink 选择 release；
- Data 的 lock 使用相邻 `eidolon_sdk` source。SDK 因此是受控 release 输入，但不是 lifecycle service；
- Kernel、Data、eidolond 各自持有 SQLite authority，当前 release 没有数据库 schema migration；
- Hub/Admin/Bootstrap 已由其他边界部署，本阶段不能顺便接管；
- Raspberry Pi 使用 Linux/aarch64 原生 Python 环境，macOS venv 不能直接发布过去。

## Decision

在 Kernel 仓库放置独立 `eidolon_deploy` 运维 package。它与 `eidolon_kernel`、`eidolon_system` 整包
independence，不被两个 runtime 进程 import，也不提供产品用户 CLI/API。

### Preparation 与 activation 分离

产品镜像或受控 staging 流水线负责网络、源码传输和目标原生 venv 构建。激活工具不下载依赖、
不 clone、不中途执行 package manager，也不接受 descriptor 提供的 shell command。

准备完成后的固定 layout 是：

```text
/srv/eidolon/releases/<release_id>/
├── eidolon_kernel/   # service component + native venv + deployment assets
├── eidolon_data/     # service component + native venv
├── eidolon_sdk/      # Data 的受控 support source，不是 service
├── release.json
└── release.json.sha256
```

`seal` 只能在 Linux/aarch64 target 上运行，记录三个完整 Git object ID、三棵 source fingerprint、两个
lock digest、两个已安装 distribution inventory digest、固定 entrypoint、固定系统资产、所需 secret、
受影响 unit 和 readiness。Wire contract 是严格 Draft 2020-12 JSON Schema；额外字段、路径逃逸、
非 allowlist 目标、远程 readiness、任意 migration 都拒绝。

Git revision 在 V1 是受控 staging 提供的 provenance label；source hash 与 environment/lock hash 才是
target preflight 实际复核的内容。SHA sidecar 防止意外或非并发一致读取，不构成签名。若未来 release
经不可信通道分发，必须先增加签名及受保护 trust root，不能把 checksum 称为 authenticity。

### 单机事务

每次 dry-run、activate 或 explicit rollback 都先持有 `/run/lock/eidolon-release.lock` 的非阻塞排他锁。
activation 在任何 host mutation 前完成全部检查：target profile、support source、component source、
lock、Python version、installed distribution inventory、entrypoint、current symlink namespace、资产 hash、
secret mode、`systemd-analyze verify`。

通过后在 root-only `/var/lib/eidolon/deployments/` 创建 snapshot，保存旧 Kernel/Data link target 和
allowlist 系统资产；secret 内容不备份。snapshot V2 对每个既有资产同时记录 UID/GID，restore 将备份
复制到同目录临时文件、恢复 ownership 后再原子替换。V1 snapshot 没有足够信息恢复 `root:eidolon`
等非默认 ownership，因此新版 loader 直接拒绝，不猜测用户名或按路径硬编码权限。随后停止
eidolond/Data/Kernel、原子安装资产并切换两个 link，reload systemd，只启动 eidolond，由其既有
desired state 重新拉起 Data/Hub/Kernel。三个固定 readiness 全部 ready 才写 `activated` receipt。

从 quiesce 开始的任一异常都会恢复旧资产和旧 link，再由旧 eidolond reconcile；恢复成功写
`rolled_back` receipt 并返回失败。恢复本身失败则返回独立 `RollbackFailed`，不伪称系统已恢复。
运维人员也可在新进程中从 snapshot 执行显式 rollback，并写 `restored` receipt。

### 数据库边界

V1 强制 `database_migrations=[]`。发布工具不连接、不快照、不迁移 Kernel/Data/Hub/eidolond SQLite。
symlink rollback 只有在新旧代码共享当前 schema 时才成立。首个数据库变更必须另写 ADR，明确 authority
owner、backup 一致性点、forward compatibility、恢复验证和失败处置，之后才可扩展 release contract。

## Consequences

收益：

- 人工顺序收敛为可测试、fail-closed、可重放证据的单机事务；
- systemd 继续管理 PID/cgroup，eidolond 继续拥有 desired state，部署工具只拥有短生命周期切换；
- SDK 的真实依赖被记录，不被错误提升成 Kernel module/service；
- Hub/Admin/Bootstrap 与所有数据库保持原有 authority 边界；
- macOS/dev 仍可测试 application/contract，只有 native preparation/seal/activation 限定产品 target。

代价和未完成项：

- V1 不是通用 installer、OTA server、artifact registry 或跨 Host orchestrator；
- V1 没有签名、TPM trust root、A/B 分区或断电原子性证明；root-owned 本机边界是明确部署假设；
- snapshot wire 已提升为 V2；开发验证期生成的 V1 snapshot 不兼容，也不能用于新版显式 rollback；
- release source staging 仍属于产品镜像流水线；待出现第二类产品载体和稳定 artifact 分发事实后再抽象；
- Raspberry Pi 已完成 dry-run、成功激活、瞬态端口故障自动回滚、跨进程显式回滚和重启恢复；后续
  release 仍必须从 clean commit 构建，并在 seal 前用 `git rev-parse` 逐项核对完整 revision。
