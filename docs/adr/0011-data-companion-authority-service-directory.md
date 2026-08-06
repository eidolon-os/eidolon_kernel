# ADR-0011: Data Companion Authority 纳入本机 Service Directory

- 状态：Accepted，代码与本地进程 E2E 已实现；Raspberry Pi 激活待部署授权
- 日期：2026-08-06

## Context

Kernel 的 Attach/Reconciliation 已只消费 Eidolon Data V2 的窄 Companion Identity GET，但生产
composition 仍保存 Data loopback 地址；与此同时 Hub 已改为通过 `eidolond` ready directory 解析。
静态 Data 地址会绕过生命周期/readiness，形成第二路由真源，也无法独立表达“Hub 可用、Data
不可用”或相反状态。

代码审计确认 Data V2 已发布：

- `GET /api/companion-authority/v1/companions/{companion_id}`；
- `GET /health`；
- producer Schema `$id=https://eidolon.dev/data/contracts/v1/companion/identity.schema.json`；
- 独占 `eidolon-system.sqlite3` 与 tracked Alembic V2 baseline；
- 独立 bearer credential `EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN`。

没有证据要求 Kernel 读取 Data DB、拥有 Companion lifecycle、引入消息总线或让 Data 成为 Hub 的
dependency。Channel 的 audio pipeline 也不经过这条低频控制面。

## Decision

Kernel 新增 Data-specific directory-routed Companion adapter，但继续只实现既有
`CompanionAuthority` Port。每次 Attach prerequisite 或 Companion reconciliation 固定 Resolve：

```text
service_id=data
endpoint_id=companion-authority.http
protocol=http
contract=https://eidolon.dev/data/contracts/v1/companion/identity.schema.json
```

只有四项完全匹配才调用解析地址上的精确 Identity GET。目录不可用、endpoint 未 ready 或 contract
漂移全部映射为 `AuthorityUnavailable`；没有静态 fallback、缓存、service proxy 或共享 DTO。Data
credential 只发送给 Data，不发送给 `eidolond`。

产品 systemd manifest 增加独立、required、default-enabled 的 Data service，但 Data、Hub、Kernel
之间不声明伪 hard dependency。`eidolond` 分别观察各自 health 并发布 endpoint。Kernel `/health`
分别报告：

- `device_mount_write_available`：只取决于 Hub endpoint；
- `companion_attachment_write_available`：只取决于 Data endpoint；
- `authoritative_store`：Kernel SQLite/projection 自身状态。

Data 数据库由 Data release 的 Alembic 部署步骤初始化，Data 进程启动只验证 schema。Kernel 和
`eidolond` 不创建、迁移或读取该数据库。systemd unit 仅启动 Data Authority 进程，使用专用非 root
账号、root-owned secret 和独立对象目录。

## Consequences

收益：

- Hub/Data 都只通过同一个机器级目录完成逻辑寻址，但仍保持各自领域 Port 与 wire contract；
- 两条写能力独立 degraded，已有 Mount 热读不会被外部 authority 故障关闭；
- Data lifecycle/DB 归属不进入 Kernel，Channel audio pipeline 零改动；
- macOS/dev 与 Raspberry Pi/Linux 继续复用同一 Kernel 代码，只更换 manifest/Host adapter 配置。

代价与边界：

- Data 当前依赖的 `eidolon_sdk` 基础安装会带入 gRPC/LiveKit 等本 endpoint 不使用的依赖；这是
  Data/SDK packaging 技术债，不应通过复制 schema、修改 Kernel 或破坏 Channel 来规避。
- 产品镜像必须显式执行 Data V2 baseline migration 并安装准确 SDK build；失败必须阻止激活，不能
  由 Kernel 自动修复。
- 动态 registration、lease/watch、多实例、跨 Host 和通用 IPC 仍无需求证据，本 ADR 不实现。
- Raspberry Pi 持久化激活会安装新 unit、扩展精确 Polkit allowlist、更新 manifest/config 并短暂
  重启 eidolond/Kernel；在该部署动作完成前，仓库实现与本地进程证据不能宣称 Pi Data 已运行。
