# ADR-0009: Kernel 只消费 Eidolon Data V2 Companion Identity Authority

- 状态：Accepted，已实现并由跨项目门禁验证
- 日期：2026-08-06
- Data 基线：`2a33894` (`refactor: complete system data v2 boundary`)

## Context

Eidolon Data V2 已把 System Data 收敛为九张低频权威表：Owner、Companion、Persona Genome、
Memory Realm catalog、Companion/Owner face metadata、Guard binding 和 Data 自己的 audit outbox。
Device admission 属于 Hub；Device Mount 与可选 Companion Attachment 属于 Kernel。Data V2 已
删除旧 Device/runtime/Event schema、旧迁移链和兼容 API，旧 `eidolon.sqlite3` 不迁移也不保留。

Kernel 的 Attachment 只需要在状态迁移前回答一个问题：指定 Companion 是否存在、是否 active，
以及是否属于 Mount 的 Owner namespace。把 Data ORM、SQLite 路径、广义 CRUD 或 Data outbox
引入 Kernel 都会制造跨 authority 写入、schema 耦合或循环依赖。

## Decision

1. Kernel 通过自己的 `CompanionAuthority` Port 消费 Data 的精确、只读、版本化接口：

   ```text
   GET /api/companion-authority/v1/companions/{companion_id}
   ```

   调用使用独立 service credential。404 是确定性业务拒绝；401/403、超时、5xx 和 wire drift
   都是 authority unavailable，不能伪造成 Companion inactive。
2. Kernel 固定自己的 consumed JSON Schema 和 wire DTO，不 import `eidolon_data`、Data ORM 或
   producer DTO。跨项目测试比较 producer/consumer 的结构语义，并以真实 Data V2 数据库和
   uvicorn 进程验证成功、Owner mismatch、缺失、并发幂等和停机失败路径。
3. Kernel 不验证或持久化 Owner profile。`owner_id` 是 Kernel namespace principal；Data 的 Owner
   资料权威与 Kernel 的 trusted ingress 授权是两个不同边界。Attachment 只比较两个 authority
   返回/持有的 opaque Owner ID。
4. Kernel 不读取或写入 `eidolon-system.sqlite3`，不调用 Data 的 application service 以外的存储
   表面，也不为已删除的 Data API、schema 或旧库提供 fallback。Data contract 不足时必须先在
   producer 增加最窄的 versioned authority API，再由 Kernel 更新 consumed contract。
5. Admin 是编排与产品入口，不是 Data、Hub 或 Kernel 的共享数据层。Admin 可以依次调用各自
   authority，但不能在一个请求里直接双写多份 SQLite 并声称原子；失败恢复使用幂等 request ID、
   CAS 和可重试 orchestration。

## Audit and write-frequency decision

Kernel 的 mount、attach、detach、unmount 和 authority reconciliation 是低频 control-plane
governance transition。它们在 Kernel 独占 SQLite 的一个短 `BEGIN IMMEDIATE` transaction 中原子
提交当前状态、幂等结果和一条本地有序审计事实。事务内没有 Data/Hub/network I/O；外部校验在
事务前完成，提交前再读 revision，以 CAS 解决竞争。

`kernel_audit_events` 是 Kernel bounded context 的本地权威历史，不是 Data V2 `audit_outbox`，
也不是全局审计 query index。Kernel 不向 Data outbox 写入，不让 Admin 打开 Kernel 数据库，也不
在业务事务内同步发布 NATS/JetStream。将来接入全局审计时，只能由独立 dispatcher 异步批量发布
已提交的本地事实，使用稳定 `event_id` 去重；transport/index 故障不得延长或回滚 Mount transaction。

高频 media、sensor、presence、STT/TTS delta、turn phase、command retry/receipt 仍属于 Channel、
Agent 或 provider 本地运行态，禁止写 Kernel 或 System Data SQLite。

## SQLite and failure model

- Kernel SQLite 使用 WAL、外键、5 秒 busy timeout、进程级独占锁和进程内 `RLock`；只有一个
  物理 writer，显式拒绝第二个进程打开同一 authority。
- 所有 mutation 使用 expected revision/CAS；相同 request ID 和 fingerprint 返回稳定 outcome，
  不产生第二条审计。不同 payload 复用 request ID fail closed。
- DB commit 先于内存 projection；commit 失败不更新 projection，projection 更新失败则从 authority
  重建。事务异常执行 rollback。
- Kernel 自己的 schema 只接受空库创建或当前精确版本；未知、部分或旧库直接拒绝，不承担 Data
  旧库迁移。Data V2 的生产 schema/version/integrity 由 Data 进程自己验证。

## Consequences

依赖方向保持为 `Kernel application -> Kernel port <- Data HTTP adapter -> Data public contract`；
Data 不依赖 Kernel，Admin 不成为 authority，因此没有循环依赖或双写。代价是 Data Authority
不可用时新的 Attach fail closed，现有 Mount 热读继续可用；周期 reconciliation 将网络故障记为
deferred，而不是错误 Detach。

Companion Authority 尚未进入已验证的 `eidolond` host manifest，因此 Kernel 暂时只有一个显式
loopback base URL 真源。等部署提供真实 supervisord/systemd target 与 readiness 后，可以像 Hub
一样改由 Kernel 自有 directory Port 解析；在此之前不能虚构 target 或同时保留静态 fallback。
