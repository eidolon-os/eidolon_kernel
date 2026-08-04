# ADR-0004: System Service 收敛到 Port/Contract，不预建 Binder runtime

- 状态：Accepted
- 日期：2026-08-04

## Context

Eidolon OS 当前已经使用 HTTP、gRPC、NATS 和 LiveKit，但代码确认它们承担不同语义：

- Kernel Device Mount 和 Hub management 是低频 HTTP/JSON 控制面；
- Channel 到 Agent 是支持增量结果和取消的 gRPC 双向流；
- NATS 用于需要 publish/ack/redelivery/replay 的异步路径；
- LiveKit 拥有 room、participant 和 media session 生命周期。

现有关键边界已经由领域 Port 隔离。例如 Kernel 的 `DeviceAuthority` / `CompanionAuthority` / `OwnerAuthorizer`、Hub 的 `ChannelProviderControl`、Agent 的 `MemoryPort` / `BodyCommandPort`。当前代码没有动态 Service Manager、跨服务 CapabilityHandle/Lease、服务按需激活或统一死亡通知，也没有同一领域 Port 必须透明运行在多种 transport 上的事实。

因此，“使用了多种协议”不能证明需要 Binder。现在增加 `ipc.call(service, method, payload)` 会抹掉 streaming、cancellation、ack、replay 和 media lifecycle 差异，最终仍需把 transport 概念泄漏回调用方。

## Decision

- System Service 的稳定边界是 `Port + Contract + Adapter`，不是 transport 或通用 RPC facade。
- Domain/Application 只依赖领域 Port；HTTP、gRPC、NATS、LiveKit、SQLite 留在 adapter/interface/composition 外层。
- JSON Schema、protobuf 或事件 schema 由对应事实拥有方维护；wire DTO 与 domain entity 显式映射。
- 只在出现实际重复时统一最小跨服务语义：稳定 ID、authenticated principal、`request_id` / `trace_id` / deadline 和领域错误分类。各协议仍映射为原生 header、metadata、status 或 event attribute。
- 不定义 universal payload envelope，不建立 `eidolon_ipc` package/daemon，不实现通用 Service Proxy、动态 Service Manager、通用消息总线或共享 blackboard。
- 不要求兄弟项目统一依赖某个源码 SDK；共享的是版本化 contract，而不是项目内部类型。

## Binder-like runtime 的进入门槛

只有代码和部署出现至少一项真实需求，才新开 ADR 评估：

1. 第三方应用或扩展以独立、不可信进程动态加载，必须发现和调用 System Service；
2. 同一 Framework API 需要在进程内、本机进程和远端 Host 之间迁移；
3. CapabilityHandle/Lease 成为稳定授权模型，需要跨服务强制执行；
4. 需要统一的服务注册、按需激活、endpoint lease 或死亡通知；
5. 多个领域接口因重复身份、deadline、错误和生命周期 glue 已产生可复现缺陷，且小型 middleware/stub 无法解决。

若门槛出现，仍应先选择最窄的本地服务管理/IPC 能力，并以威胁模型和基准决定 UDS、gRPC 或其他 transport；不预先锁定 Binder 实现。

## Consequences

当前架构保留各协议擅长的交互模型，同时让业务语义稳定在领域 Port。代价是不同 adapter 仍需做明确的 context 和错误映射；这种重复是可见且可测试的边界成本，比一个泄漏语义的全局 IPC facade 更容易演进。
