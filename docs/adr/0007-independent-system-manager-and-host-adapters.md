# ADR-0007: 独立 eidolond 进程与 Host Service Adapter

- 状态：Accepted，第一阶段已实现
- 日期：2026-08-06

## Context

代码审计确认了三个已经发生的问题，而不是为分布式预建机制：

1. Mac/dev 的服务生命周期由 `eidolon_admin/deploy/dev/run_all.sh` 和 Admin 内部
   supervisord XML-RPC client 控制，Admin 因而成为事实上的 OS 生命周期权威；
2. Admin `ports.yaml` 明确只是各子项目配置的聚合，Kernel、Agent、Channel 仍分别保存
   Hub、Data、Memory、LiveKit、Kernel 等地址；
3. Agent 的 Memory discovery 是领域专用 routing contract，不能被提升为通用系统目录。

目标是建立一个本机、headless、可在树莓派/Linux 与 macOS/dev 使用相同应用代码的系统服务
控制面。目标不是 Nacos、Binder、服务网格或通用配置中心。

## Decision

### 两个进程、两个 bounded context

同一个 Git 仓库发布两个互不 import 的 Python package 和进程：

- `eidolon_kernel`：Sovereign Kernel，继续拥有 Device Mount 等稳定 OS namespace 事实；
- `eidolon_system` / `eidolond`：机器级 System Manager，拥有服务期望状态、reconciliation、
  observed directory 与系统操作审计。

Import Linter 与 AST 架构测试同时阻止两个 package 相互导入。共享只能发生在版本化 wire
contract 和调用方自有 Port，不能共享 domain class 或 composition。

### Eidolon 拥有策略，宿主机拥有进程机制

`eidolond` 不直接 `fork` 业务进程。Application 只依赖 `HostServiceSupervisor` Port：

- Raspberry Pi/Linux adapter 使用 `systemctl`；systemd 继续拥有 PID、cgroup、信号、失败重启
  和宿主机开关机顺序；
- macOS/dev adapter 使用当前 supervisord 的 `supervisorctl`，作为迁移期执行器；
- adapter 使用 argv 形式的异步子进程调用，不执行 shell，不接受 HTTP body 提供任意命令或
  target；target 只能来自经过 Schema 校验的安装清单。

systemd/supervisord 是部署 adapter，不得进入 Domain、Application 或 Port。当前 macOS 只复用
已经存在的 supervisord，不声称实现 launchd adapter；若以后 Mac 成为产品载体再按同一 Port 增加。

### Stable desired state 与 ephemeral observed state 分离

独占 `eidolond.sqlite3` 只持久化：

- 每个已安装 service 的 enabled/disabled、revision、updated_at；
- 全局幂等 request、canonical fingerprint；
- 稳定递增 audit position。

PID、health、readiness、临时 endpoint 可用性不持久化。`eidolond` 每次启动都从 Host 与
readiness probe 重建 typed in-memory directory；只有 `ready` service 的 endpoint 可 Resolve。
SQLite 与 Kernel Device Mount SQLite 完全独立。

### Controller-derived publication，不预建 lease

第一阶段每个 service 在单 Host 最多一个受管理实例。安装 manifest 声明：

- 稳定 `service_id`；
- host driver 到受限 target 的映射；
- 显式 dependency；
- endpoint ID、原生 protocol、address、contract 和可选 HTTP readiness URL。

`eidolond` 已经启动并观察这些进程，因此 readiness 通过后由控制器发布 endpoint。服务不需要
增加 Register/Heartbeat/Renew SDK。只有出现动态端口、多实例、远端 Host 或不受本机控制器
管理的 provider，才另开 ADR 增加 lease/watch。

### Machine scope，不引入 Owner 或 JWT

System service 是机器级控制面，不属于 Owner namespace，contract 中禁止 `owner_id`。V1 API
只允许 loopback 或 Unix Domain Socket。产品部署应使用 UDS 文件权限限制调用者；不把本机
header、Owner token 或新 JWT 包装成伪安全。远程用户和移动端必须终止在产品 ingress，不能
直接访问 `eidolond`。

## HTTP/JSON V1

低频控制面继续使用版本化 HTTP/JSON：

- `GET /api/system/v1/services`
- `GET /api/system/v1/services/{service_id}`
- `GET /api/system/v1/services/{service_id}/endpoints/{endpoint_id}`
- `POST .../{enable|disable|restart}`，要求 request ID 与 expected revision
- `GET /api/system/v1/audit/events`

JSON Schema 是 manifest 与 HTTP wire contract 的规范来源；Pydantic binding 只负责运行时
normalization，mapper 显式转换 domain/wire。

`enable/disable` 修改持久化 desired state；`restart` 是不改变 desired revision 的幂等操作。
没有独立的临时 `start/stop`，避免下一轮 reconciliation 与人工命令互相打架。required service
不可 Disable；存在 enabled direct dependent 时必须先 Disable dependent。

## Bootstrap 与部署决定

- Host init 负责启动和失败拉起 `eidolond`；`eidolond` 不能负责启动自己。
- 产品 systemd 只应自动 enable `eidolond`；其他 Eidolon units 由 System Manager 按 desired
  state 请求启动，避免 systemd target 与 SQLite 成为双 desired-state 权威。
- macOS/dev 暂时复用现有 supervisord daemon。Admin 迁移完成前不得同时让 Admin symlink 与
  `eidolond` 修改同一服务的 enabled 状态。
- 默认 macOS/dev manifest 只纳入代码已经确认 target、health 与 authority contract 的 Hub；
  systemd manifest 明确是待产品镜像验证 unit name 的部署 example。Kernel 自身
  尚未接入当前 dev supervisord，Agent/Channel/Memory 的完整启动依赖和 readiness contract 也未
  统一；在事实稳定前不猜测并固化顺序。

## Consequences

收益：

- Endpoint 与生命周期有清晰 OS 控制面，不再归 Admin 产品层所有；
- 树莓派与 Mac 使用同一 domain/application/contracts，只替换 Host adapter 与部署配置；
- 保留 HTTP、gRPC、NATS、LiveKit 的原生语义，不产生通用 IPC；
- 后续若真实进入多 Host，消费者 Port 可更换 adapter，不需要改领域调用。

代价与 blocker：

- 当前只是首个 Hub service 闭环，尚未接管完整 dev stack；
- 树莓派镜像仍需安装实际 systemd unit、专用运行用户/组和 UDS 权限；以 root 运行且开放
  loopback mutation API 不可作为最终产品部署；
- Admin 必须在迁移后降为 `eidolond` client，才能删除现有 supervisor authority；
- Kernel→Hub 的静态 URL 尚未切换到 `ServiceDirectoryPort`，应在目录契约稳定后作为下一个
  独立消费者迁移，不在本 ADR 中制造双 fallback 真源。
