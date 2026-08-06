# ADR-0008: Kernel 通过本机 System Service Directory 解析 Hub

- 状态：Accepted，已实现
- 日期：2026-08-06

## Context

`eidolond` 已拥有单机受管服务的 observed readiness 与 ready endpoint directory，但 Kernel 的
Hub consumer 仍从 `hub.base_url` 读取静态地址。这产生两个不一致事实：`eidolond` 可以判定 Hub
未 ready 或发布新地址，Kernel 却仍绕过目录访问旧地址。Device Mount 是低频控制面，不需要为此
引入 watch、缓存、通用 IPC 或消息总线。

Kernel 与 `eidolon_system` 必须继续是独立 package/process。共享 producer DTO 或 domain class 会
把进程边界退化成源码耦合；直接读取 `eidolond` SQLite 则会制造第二个 observed-state reader。

## Decision

Kernel 定义自己的窄 `SystemServiceDirectory` Port、`ResolvedServiceEndpoint` DTO、固定 consumed
JSON Schema 和显式 mapper。HTTP adapter 只消费：

```text
GET /api/system/v1/services/{service_id}/endpoints/{endpoint_id}
```

生产 composition 使用 UDS bootstrap 访问本机 `eidolond`；`eidolond` 预绑定限制为 `0600`/`0660`
的 socket 并将 fd 交给 HTTP server。loopback HTTP 只用于显式开发配置。
Kernel package 不 import `eidolon_system`，两者只在版本化 HTTP/JSON wire contract 上相遇。

Hub `DeviceAuthority` 固定请求：

- `service_id=hub`
- `endpoint_id=device-authority.http`
- `protocol=http`
- `contract=eidolon.hub.device-directory.v1`

每次 Mount prerequisite 或 reconciliation 调用都重新 Resolve。解析结果的 identity、protocol 和
contract 必须完全匹配，之后才由既有 Hub adapter 执行 owner-scoped Device GET。目录不可连接、
endpoint 未 ready、wire 漂移或约束不匹配全部 fail closed 为 authority unavailable；没有静态地址
fallback。Hub credential 仍只属于 Hub 已发布的精确 GET，不发送给 `eidolond`。

`GET /health` 使用相同 Resolve 判定 Device Mount 写 readiness。目录暂不可用不会否定 SQLite
authoritative store，也不阻止已有 projection 热读，因此 Kernel 可以先于 Hub ready 启动，但会明确
报告 degraded，且 mutation 不会绕过 prerequisite。

## Why per-call Resolve

Device Mount 与周期 reconciliation 都是低频控制面。逐次 Resolve 直接继承 `eidolond` 当前
readiness，避免在 Kernel 新增 TTL、失效、watch 和并发缓存语义。只有测量证明目录调用成为真实
瓶颈时，才以新的证据设计缓存；不能提前引入双状态。

## Consequences

收益：

- 删除 Kernel→Hub 静态地址与目录的双真源；
- Hub degraded 后不再继续向旧 endpoint 写入；
- 保持 Kernel Port 与进程独立，不把 `eidolond` 变成业务代理或 Binder；
- UDS/loopback bootstrap 可由同一代码适配 macOS/dev 与 Raspberry Pi/Linux。

剩余边界：

- Companion authority 尚未成为 `eidolond` 中经过生命周期与 readiness 验证的服务，继续使用现有
  精确配置；在 producer/host target 稳定前不做形式化迁移。
- 真实隔离 supervisord E2E 已验证 `eidolond` 可接管 Kernel 生命周期；当前 Admin 默认
  supervisord 尚未安装 Kernel program，所以 macOS/dev 仍是部署接线 blocker，不是 adapter blocker。
- Raspberry Pi profile 已在 Debian 13 / systemd 257 真机完成安装、目录解析、Device Mount 与受管
  Kernel restart 后的持久化恢复验证。
- 动态注册、lease/watch、多实例和跨 Host 仍没有需求证据，本决策不实现。
