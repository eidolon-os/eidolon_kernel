# ADR-0013: Eidolon OS 单机统一 Release V2

- 状态：Accepted；隔离实现与测试完成，真实 Raspberry Pi 激活待显式授权
- 日期：2026-08-07
- Supersedes：ADR-0012 的 release component 范围；保留其 preparation/activation 分离和数据库边界
- Extended by：ADR-0015 的 Agent/Channel/Memory/NATS/LiveKit 完整产品运行图

## Context

当前代码证明产品单机启动图不止 Kernel/Data：eidolond 的 manifest 同时管理 Data、Hub、Kernel；Admin
仓库提供独立的 operator API、always-on Bootstrap 与 pinned HTTPS Local API。V1 descriptor 没有 Hub
component、`eidolon-hub.service`、`hub.yaml`、Admin component 或其系统资产，却可能在 Kernel/Data
健康后写出 `activated`。此外 Hub 与 Local API 的真实 `/health` 响应是 `status=ok`，V1 探针固定只接受
`ready`。这两点都不能通过运维说明弥补。

Admin 仍是控制/编排面：它通过 eidolond 发现 Data/Hub/Kernel 的公开 HTTP 契约，不打开任何 producer
SQLite。Bootstrap 的 Host identity/commissioning SQLite 独立归 Bootstrap bounded context；Local API 是
其唯一拥有 socket access 的产品 ingress。统一发布不改变这些 authority 归属。

## Decision

Release descriptor wire contract 提升为严格 V2，并拒绝 V1。固定 service components 为
`eidolon_kernel`、`eidolon_data`、`eidolon_hub`、`eidolon_admin`；`eidolon_sdk` 仍只是 support source。
V2 记录四套 source/lock/environment/entrypoint fingerprint、14 个跨 Kernel/Admin 仓库的 allowlist 系统
资产、6 个 mode `0600` 前置文件、6 个受影响 unit 与 6 个逐项声明期望状态的 readiness。

系统资产 source mapping 同时固定 component ID 和相对路径，防止把同名文件换到另一 component。Avahi
XML 虽以 `.service` 结尾，但不交给 `systemd-analyze verify`；只有 `/etc/systemd/system` destinations
参与 unit verification。

Quiesce 顺序固定为 Admin → Local API → Bootstrap → eidolond → Data/Hub/Kernel，避免外部入口在切换
期间继续接单，也避免 eidolond 与 deployer 同时管理 child lifecycle。启动顺序固定为 Bootstrap →
eidolond → Local API → Admin；Data/Hub/Kernel 仍仅由 eidolond desired state 启动。

Local API readiness 使用 loopback HTTPS 且不验证自签名证书链。这只用于 root operator 在同机证明进程
可响应并能访问 Bootstrap；产品客户端仍必须按现有契约验证 Host ID 和 Host-signed TLS SPKI，不能复用
该探针作为认证。

`doctor` 是只读 post-flight：它持有同一 activation lock，重新执行 sealed preflight，要求四个 current
link 精确指向该 release，并验证七个 units active 和全部 readiness。`deploy` 是 `activate` 的运维命名
入口，保留同一事务与自动恢复语义。

## Consequences

- 一个成功 receipt 现在覆盖 Data/Hub/Kernel/Admin 的代码、单位文件、配置模板和本机产品入口。
- 发布仍不拥有任何业务数据库、Bootstrap identity 或 secret 内容；它只验证固定前置文件存在且 mode
  正确。
- V1 descriptor 会 fail closed，不能被误报为全栈发布。
- 这仍是 target-native prepared-release 激活器，不是 source downloader、artifact signer 或 first-install
  provisioner。真正从工作站一键部署到新 Pi 还需要独立实现可信 artifact 传输、用户/目录、Data V2
  baseline、Host identity 与 secret provisioning，并在 Pi 上执行故障注入/reboot 验证。
