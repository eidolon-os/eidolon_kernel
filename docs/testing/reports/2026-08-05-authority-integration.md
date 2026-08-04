# Authority 集成与对账测试报告

- 日期：2026-08-05
- 范围：Kernel 当前代码、真实 Hub/Data producer 联合契约、周期对账

## 已验证

- Hub consumer 只调用 Owner-scoped 精确 Device Get，并使用 Hub 限定为该操作的 opaque reader token；
- Data consumer 只调用版本化 Companion Identity Get，严格过滤 profile/runtime metadata；
- producer response 先通过 Kernel 固定 consumed JSON Schema，再经严格 binding 和显式 mapper 进入 domain；
- Device revoked/缺失或 Companion inactive/缺失会以 CAS 生成 inactive tombstone 和审计；
- Authority 网络、认证、5xx 或契约故障只延后对账，不伪造撤销；
- worker 启动即执行、异常后继续下一轮，shutdown 可重复且会取消任务；
- 临时 fail-closed Companion adapter 已从 production 删除。

## 结果

```text
Kernel full: 82 passed in 3.18s
Kernel branch coverage: 94.74%
Hub full: 130 passed in 12.70s
Hub Domain/Application branch coverage: 97.48%
Data full: 55 passed in 37.68s
```

Kernel/Hub/Data 全量 Ruff、Kernel/Hub Import Linter、依赖锁检查和三个发布包构建均通过；Data wheel 已直接确认包含 Companion Identity Schema。

## 证据边界

联合测试使用各仓库真实 Composition/Authority App、真实 HTTP/ASGI 契约和独占临时 SQLite，但仍是单进程测试环境。它不证明进程管理器已启动 Data Authority、真实 TLS/代理、真实小程序 Approval→Mount 编排、真实设备或 `eidolon_channel` Provider。以上仍需产品集成验收，不能记为 Kernel 功能已验证。
