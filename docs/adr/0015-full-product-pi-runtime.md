# ADR-0015: Full-product Raspberry Pi runtime release

- 状态：Accepted；隔离实现完成，真实 Raspberry Pi 验收待明确授权
- 日期：2026-08-07
- Extends：ADR-0013、ADR-0014

## Context

既有统一 release 只覆盖 Kernel、Data、Hub、Admin 以及 Bootstrap/Local API。代码证据显示手机 App 的
实际语音/Companion 路径还依赖 NATS、LiveKit、Memory、Agent 与 Channel；仅核心 8 个进程健康不能称为
“所有服务 + App 开箱即管”。同时这些服务在 Mac 上的 supervisord 开发进程不能直接当成 Pi 产品契约。

Admin 仍是控制/编排面，不拥有 Data/Hub/Kernel/Agent/Channel/Memory 的权威状态。部署边界不得用完整
产品拓扑为跨库访问、权威表复制或分布式原子性背书。

## Decision

V2 descriptor 的固定 source 输入扩展为 Kernel、Data、Hub、Admin、Agent、Channel、Memory、SDK；
前 7 个是独立 component，SDK 是 support source。发布事务固定 22 个系统资产、11 个 required secret、
13 个 affected unit 和 12 个 readiness，并把 7 个 component link 一起 snapshot/switch/restore。

`eidolond` 的 system-service manifest 增加 NATS、LiveKit、Memory Supervisor/Discovery、Agent 与 Channel。
NATS/LiveKit 是受 systemd 管理的固定版本外部二进制；Memory、Agent、Channel 使用各自仓库的精确提交和
原生 venv。依赖方向是：NATS → Memory → Agent → Channel；LiveKit → Hub/Channel；Data/Kernel 是
Channel 的公开契约依赖。没有服务通过部署工具直连兄弟数据库。

Channel 的 8 个 commit-pinned Git LFS pointer 由 bundle builder 直接通过 `git lfs smudge` 导出，不读取
working tree；输出必须匹配 pointer 的 SHA-256 与 size，随后才写入 source archive。缺失 LFS object、
网络失败、digest drift 或未 hydration pointer 都被 gate 拒绝。
Readiness 类型扩展为 TCP、generic HTTP 2xx 和 systemd active，避免用假的 JSON 健康语义包装外部服务。

## Consequences

- 一次成功 release receipt 覆盖手机 App 所需的正式后端拓扑，而非只覆盖核心控制面。
- `client-web`、Audit worker、Vision 和手机安装包仍不是此 descriptor 的产品 unit；它们不是手机 App 管理
  Host 的必要后端，若以后进入正式 Pi 镜像，必须先各自提供固定构建、unit、secret 与 readiness 契约。
- First install 与 Debian/BlueZ/NetworkManager/NATS/LiveKit 基础 provision 继续由独立 `eidolon_ops`
  编排；Kernel release 事务不升级成通用包管理器。
- 真实 Pi/手机尚未执行，因此 `app-ready` 只能证明 Host 侧门禁，不能替代 BLE、Host proof、TLS SPKI、
  Controller claim、Wi-Fi checkpoint 与 Workspace setup 的真实端到端验收。
