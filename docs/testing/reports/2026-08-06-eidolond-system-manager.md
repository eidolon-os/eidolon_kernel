# eidolond System Manager 第一阶段测试报告

- 日期：2026-08-06
- 范围：`eidolon_kernel` 仓库内新增的独立 `eidolon_system` package/process
- 目标：建立本机服务 desired-state、Host reconciliation、ready directory 和系统操作审计的
  最小纵向闭环，不扩张 Device Mount Kernel，不引入 Binder/Nacos/NATS dependency

## 已实现并验证

- `eidolon_kernel` 与 `eidolon_system` 是两个互不 import 的 package；各自拥有独立
  domain/application/ports/adapters/interfaces/composition/contracts。
- Draft 2020-12 JSON Schema 控制 service manifest 和全部 HTTP V1 wire shape；binding 与
  domain entity 显式映射。
- Manifest 校验稳定 service/endpoint ID、Host target、dependency existence/uniqueness 和有向无环
  拓扑；启动按 dependency order，停止按 reverse order。
- 独占 `eidolond.sqlite3` 保存 desired state、revision/CAS、全局幂等 request outcome 和稳定递增
  audit position；observed state 只在 typed in-memory directory。
- required service 不可 Disable；存在 enabled direct dependent 时 dependency fail closed；
  Enable/Disable 后立即 reconcile，Restart 幂等且不修改 desired revision。
- systemd 与 supervisord adapter 只接收 manifest target，通过 `create_subprocess_exec(argv)` 调用，
  不经过 shell；超时进程会被 kill/reap。
- readiness 未通过、dependency blocked 或 Host operation 失败时 endpoint 不可 Resolve；失败作为
  observed status 暴露，不伪造持久化在线状态。
- 配置严格限制 loopback/UDS；System Service API 是 machine scope，wire/domain 没有 Owner。
- 仓库内 macOS/dev supervisord 与 Raspberry Pi/Linux systemd 配置使用同一应用代码，只替换
  Host adapter 和路径。默认 dev manifest 只纳入已由当前代码确认的 Hub target/health/contract；
  systemd unit name 只存在于明确标注为 proposal 的 example 中。

## 验证命令与结果

```text
uv run pytest -q
114 passed

uv run pytest --cov=eidolon_kernel --cov=eidolon_system --cov-report=term-missing -q
114 passed; branch coverage 92% (gate 90%)

uv run ruff check eidolon_kernel eidolon_system tests scripts
All checks passed

uv run lint-imports
8 contracts kept, 0 broken
```

## 测试分层

- unit：catalog/topology、不变量、desired-state lifecycle、readiness/blocked/failure；
- contract：全部 JSON Schema、严格 manifest 与仓库配置 profile；
- component：SQLite 重启/锁/CAS/幂等/audit、systemd/supervisord argv adapter、subprocess timeout；
- functional：HTTP List/Get/Resolve/Enable/Disable/Restart/Audit 和稳定错误映射；
- architecture：双 package independence、inward imports、Host platform 隔离；
- regression：原 Device Mount unit/contract/component/functional/E2E 全量保留。

## 剩余 blocker / 下一步门槛

1. 当前 dev supervisord 没有 Kernel program，默认 System Manager seed 只接管 Hub 闭环；正式把
   Kernel 置于 `eidolond` desired state 前，需要先提供经过部署验证的 macOS 与 systemd unit。
2. Admin 仍直接拥有 supervisord enable/disable surface。迁移期间不得由 Admin 和 `eidolond`
   同时修改同一 service desired state；下一步应让 Admin 成为 System Manager consumer。
3. Kernel→Hub 仍使用静态 `hub.base_url`。目录契约稳定后应由 Kernel 自己定义
   `ServiceDirectoryPort` 和 consumed wire contract，再删除静态地址；不建立永久 fallback 双真源。
4. Agent、Channel、Memory、NATS、LiveKit 尚未纳入 seed manifest。只有各自真实 dependency、
   readiness 和 host target 经代码/部署确认后才逐项接入；不能根据端口表猜测。
5. 树莓派镜像还需确定 systemd unit 安装、运行用户/组、polkit/root 权限和 UDS mode。当前代码
   不把 loopback HTTP + root 伪装成最终安全部署。
6. 没有动态 Register/Heartbeat/Lease/Watch、多实例或跨 Host registry；出现真实需求前不实现。
