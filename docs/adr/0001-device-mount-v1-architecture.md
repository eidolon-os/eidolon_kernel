# ADR-0001: Device Mount V1 采用本机 SQLite + typed projection + HTTP/JSON

- 状态：Accepted
- 日期：2026-08-04

## Context

Sovereign Microkernel 只承载必须全局权威、跨服务一致的机制。Hub 已拥有 Device onboarding/registry/policy，Companion 生命周期由外部事实拥有方管理；Kernel 需要拥有的是 Device 进入全局 OS namespace 后的 mount relation、revision、authoritative state 和 audit。

Mount/Resolve 是低频 metadata 控制面，不是设备 command/state/media 热路径。当前部署目标是单 Host、单进程，没有多实例一致性或跨 Host fan-out 的实测需求。

## Decision

- 分层固定为 domain/application/ports/adapters/interfaces/composition/contracts，依赖只向内。
- JSON Schema Draft 2020-12 是 V1 wire source；DTO 与 Domain Entity 显式映射。
- Kernel 独占一个 SQLite 文件。当前 mount、幂等结果和 audit 在一个事务提交；revision 作为 CAS fencing。
- 每个 Device 保存一条当前 active fact 或 inactive tombstone；audit 保存不可变历史，position 单调递增。
- typed in-memory projection 从 mount table 重建，Get/Resolve/List 走内存；SQLite 是唯一 authority。
- 对外使用 `/api/kernel/v1` HTTP/JSON。没有基准证据前不增加 gRPC。
- 不引入 NATS、Redis、通用 bus 或共享 blackboard。

## Consequences

单机写入模型简单且可审计，projection 损坏不会丢失事实。控制面不会污染媒体或设备协议边界。代价是 V1 只支持单进程独占文件，跨 Host 扩展需要在出现真实需求后重新决策；开发期旧数据库直接拒绝，不提供 migration。
