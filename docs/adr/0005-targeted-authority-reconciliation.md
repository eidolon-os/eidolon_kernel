# ADR-0005: Active Mount 使用定向 Authority 对账

- 状态：Accepted
- 日期：2026-08-05

## Context

Device Mount 引用 Hub 的 Device admission 和 Data 的 Companion lifecycle。Mount 创建时的同步校验只能证明当时成立；之后 Device 可以 revoked，Companion 可以 inactive 或被删除。Hub、Data 与 Kernel 都是各自事实的唯一权威，当前产品又是 local-only 单机控制面，没有证据支持为这一条低频失效路径引入 NATS、通用事件总线、分布式事务或 Hub→Kernel callback。

## Decision

Kernel production composition 启动一个小型周期 worker，立即并按配置间隔扫描当前 active Mount。每条 Mount 只通过两个窄 Authority 精确 GET 重验：

1. Hub Device 必须存在、`approved` 且 Owner 匹配；
2. Companion 必须存在、`active` 且 Owner 匹配。

权威拒绝会生成确定性内部 request ID，以当前 Mount revision 做 CAS，将记录变为 inactive tombstone，并写入 `eidolon.kernel.device-unmounted-by-authority.v1` 审计。等待外部 Authority 后必须重新读取 revision；并发用户 mutation 优先，本轮跳过，下一轮重验。

网络、认证、5xx、超时和 consumed-contract 错误属于 `AuthorityUnavailable`，只记为 deferred，保持 Mount 原状态。基础设施故障不能被伪造成业务撤销。

## Consequences

- 撤销收敛窗口上界由 `reconciliation.interval_seconds` 决定，默认 30 秒；不是即时通知。
- 每轮外部调用数与 active Mount 数线性相关；当前本地低频控制面可接受。只有实际规模或延迟测量不能满足目标时，才评估批量 Authority API 或权威事件流。
- Kernel 不反向修改 Hub/Data，也不拥有它们的 lifecycle；tombstone 只表达 Mount 已失效。
- 不新增通用总线、共享数据库、callback endpoint 或跨服务两阶段提交。
