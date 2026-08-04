# Device Mount V1 测试与结构反思报告

> 历史基线：Companion Authority blocker 与 lifecycle reconciliation 缺口已于
> 2026-08-05 关闭。当前结果见
> [Authority 集成与对账测试报告](2026-08-05-authority-integration.md)。

- 日期：2026-08-04
- 范围：首个 Device Mount 最小纵向闭环
- Python：3.13.13（项目声明支持 `>=3.11`）

## 验证结果

| Gate | 命令 | 结果 |
|---|---|---|
| Lint | `uv run ruff check eidolon_kernel tests scripts` | Passed |
| Layer contracts | `uv run lint-imports` | 4 contracts kept, 0 broken |
| Full tests | `uv run pytest -q` | 66 passed |
| Branch coverage | `uv run pytest --cov=eidolon_kernel --cov-report=term-missing -q` | 96.07%，高于 90% gate（66 passed） |

测试目录覆盖：

- unit：aggregate transition、fingerprint、authority validation、explicit remount、CAS、idempotency、projection query/error boundary；
- contract：全部 Draft 2020-12 schema、runtime DTO、严格 additional properties、Hub consumed contract；
- component：SQLite 原子事务/进程锁/旧库拒绝/重启、typed projection、trusted-local authorizer、Hub HTTP consumer 的 URL/header/error/contract；
- functional：HTTP Mount/Get/Resolve/List/Unmount/Audit、scope 隔离、外部 authority 与 identity fail-closed；
- E2E：真实 Kernel SQLite 跨进程重建后 Resolve → Remount → Unmount → ordered Audit；
- architecture：真实 import 方向、core 框架独立、SQLite confinement、独立仓库/无兄弟源码依赖、禁止 NATS/Redis/gRPC/LiveKit/MQTT 等越界依赖。

E2E 使用明确注入的 Companion authority fake，因为生产 Companion contract blocker 尚未解除。fake 不在 production package/composition 中。

## 测试驱动的结构反思

1. **Audit 必须保存事件时快照，而不能从当前 mount 反推历史。** 审计表因此独立保存 `mount_created_at`、owner、revision、active、request 和 fingerprint；remount 后读取旧事件仍得到当时事实。
2. **Projection 更新失败不应把已经提交的 DB mutation 伪装成失败。** application 在 authority commit 成功后尝试增量更新；若 projection adapter 异常，就从 `store.list_all()` 重建。反向测试也保证 DB commit 失败时内存完全不更新。
3. **幂等检查必须早于外部 authority 复验。** 已成功请求在 Hub 后续 revoked 或 Companion 后续 inactive 时仍返回原稳定 outcome，不产生第二个 audit position；新的 request 仍执行最新 authority 校验。
4. **Consumed contract 需要严格到 Manifest item。** Hub response 的顶层和 property/action/event/media item 都按当前稳定 schema 校验，防止 adapter 悄悄接受 Provider binding 或不完整 capability shape。
5. **Composition 失败路径不能先占 DB 再读 secret。** production composition 先读取 Hub token，再取得 SQLite 进程锁，避免缺 secret 时泄漏 authority lock。
6. **Owner scope 必须来自 security context，而不是请求目标参数。** Mount/Unmount body 和所有 read query 都不能另传 owner；Get/Resolve 在 application 内再次比较 mount owner，不同 Owner 只能得到 404/空列表，不能枚举 namespace。
7. **Owner transfer 必须在 domain、application 和 SQLite CAS 三层拒绝。** 即使调用方知道全局 device ID 和 tombstone revision，也不能通过 remount 把记录移入另一 Owner namespace。

## 已验证的不变量

- 每个 Device 只有一条当前记录且最多一个 active mount；Companion 没有单设备限制。
- 每个 mutation 使用 expected revision，revision 严格递增。
- active mount 只能显式 replace；inactive tombstone 可带当前 revision 重挂。
- Mount 的 Owner 在整个记录生命周期内不可变；Device 与 Companion 必须和 Mount 处于同一 Owner namespace。
- request ID 全局唯一；相同 fingerprint 重放稳定 outcome，不同 payload 冲突。
- mount、request outcome、audit 在一个 SQLite transaction 中提交。
- audit position 稳定递增，owner-scoped `after_position` 可增量读取。
- projection 可从唯一 DB authority 重建，且没有 projection table。

## 剩余风险 / Blocker

1. Companion authority 尚无稳定 versioned contract，production Mount fail closed（ADR-0002）。
2. Kernel 没有直接不可信网络认证；trusted-local Owner hint 只适用于 loopback/已认证同机 ingress。固定单机 local-only 部署接受该 threat model；直接网络暴露或跨 Host 前必须替换 authorizer（ADR-0003）。
3. Kernel 不拥有 Owner profile/account authority；当前也没有需求证明 Device Mount 需要读取 Owner 资料。若未来需要 Owner lifecycle，必须先由事实拥有方发布窄 contract，不能直连兄弟 DB。
4. 外部 Device/Companion authority 校验与 Kernel commit 之间没有分布式事务；事实可在校验后立即变化。后续需定义 revoke/inactivate 到 mount reconciliation 的契约，而不是引入通用 bus。
5. SQLite + projection 只支持单 Host、单进程；当前没有多实例需求证据。
6. 开发期不支持旧 DB migration，这是锁定的项目边界，不是遗漏。

## 下一步门槛

- Companion 事实拥有方先发布严格 read/lifecycle/auth contract，Kernel 再增加 production consumer 和 provider/consumer contract test。
- 仅当 Kernel 需要直接接入不可信网络、跨 Host 或同机不可信进程时，先定义可验证 principal 与信任通道，再替换 authorizer adapter；不新增通用 JWT。
- Hub 对接任务根据稳定 Kernel contract 实现 onboarding 后 Mount，以及 revoked 后的定向 reconciliation；保持 Hub 与 Kernel DB 各自独占。
- 收集真实 Mount/Resolve 延迟和故障数据；只有证据表明 HTTP/JSON 或单机 authority 不足时再作 transport/storage ADR。
