# ADR-0006: Device Mount 与 Companion/Channel 能力解耦

- 状态：Accepted
- 日期：2026-08-05

## Context

当前代码中曾把 `bound_companion_id` 同时用于 Device 准入、默认 Companion、音频会话
路由和设备能力发现。这会错误排除三种正常产品形态：只提供 data/control 的 Device、
没有物理 Device 的虚拟 Companion，以及同一 Companion 使用多个 Device/Channel
Provider。Data 中的物理 Device 表和 NATS runtime-device blackboard 还会与 Hub/Kernel
形成第二套权威。

## Decision

1. Kernel 的 Device Mount 只表示 Hub 已准入 Device 进入 Owner namespace。它不要求
   Companion，也不解释 audio/video/data capability。
2. `attached_companion_id` 是 Mount 上一个可选、显式、同 Owner 的默认关联。它不是
   Device 所有权、ACL、永久配对或当前 Channel session target；Attach/Detach 使用同一
   revision/CAS 和审计流。
3. Channel 在入口处形成两种上下文：
   - `DeviceConnectionContext`：Owner-scoped mounted Device，可无 Companion，供未来
     data/control Provider 使用；
   - `CompanionInteractionContext`：已选择并校验 Companion runtime，可来自带 attachment
     的 Device，也可来自完全没有 Device 的虚拟 Companion。
   audio pipeline 只接收第二种完整上下文；本 ADR 不修改其 STT/TTS/VAD/interrupt 实现。
4. Device capability declaration 属于 Hub manifest，实时连接、媒体、状态和命令属于
   Channel Provider。Kernel 不复制 capability catalog 或在线状态。
5. Guard 是 capability 驱动的产品控制面配置，不是特殊 Device kind，也不能批准、认领、
   撤销、mount 或 attach Device。Data 的 Guard binding 只能引用设备并保存 Guard 自己的
   policy/runtime desired state。现有 `guard_companion_id` 与兼容 Persona/Memory workspace
   是 legacy consumer contract，不提升为 Kernel 概念。
6. Agent 保留 capability directory/command 的领域 Port，但在 Channel 尚未发布稳定契约前
   不提供 adapter。已从当前 Hub 删除的 command API 和 NATS device blackboard 不作为
   fallback。
7. 不建立通用 ResourceGraph、Assignment service、共享 Blackboard 或统一 IPC/Binder。
   各消费者只使用其领域 Port + versioned Contract + Adapter。

## Current facts and remaining gates

- Data V2 基线 `2a33894` 已删除 `DeviceRow`/`DevicesRepository`、runtime/Event API、旧迁移链与
  compatibility surface。Kernel 不读取 Data SQLite、不 import Data package，也不为旧
  `eidolon.sqlite3` 增加 fallback；Device admission 只来自 Hub，Mount/Attachment 只写 Kernel
  自己的 authority。
- Data V2 对 Kernel 保留的唯一公开面是经过 service credential 认证的精确 Companion Identity
  GET。Kernel 固定消费自己的 strict Schema，只接受 `companion_id`、`owner_id` 与归一化后的
  `active|inactive` lifecycle，不消费 Data ORM、Owner profile、Persona、Memory Realm、Guard 或
  Data audit outbox。
- Channel 的 Kernel Mount consumer 默认关闭，因为当前本地 composition 尚未启动
  Kernel，Provider 也尚未稳定写入受信 `owner_id`。在这两个条件满足并有跨项目 E2E 前
  不得默认开启。
- Admin/Channel/Agent 中仍引用旧 Data schema 或直接读取 Data SQLite 的路径是各自的 V2
  consumer 迁移债务，不能在 Kernel 内用兼容 API、共享 ORM 或反向写 Data 来掩盖。
- Agent body control 只能在 Channel 发布 Owner-scoped provider listing、幂等 command
  submission 和 terminal receipt 契约后重新启用。

## Consequences

Device、Companion 与 Channel 能独立演进，音频链路继续只处理完整 Companion runtime，
Kernel 保持小而权威。代价是迁移期仍有明确标注的 legacy Data/Admin 表面和一个关闭的
Channel/Agent 集成开关；它们由上述契约/E2E 门槛解除，而不是用新总线掩盖。
