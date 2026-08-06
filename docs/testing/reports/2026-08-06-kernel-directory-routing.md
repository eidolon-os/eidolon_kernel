# Kernel 消费 System Service Directory 测试报告

- 日期：2026-08-06
- 范围：Kernel→`eidolond`→Hub Device Authority 路由
- 目标：删除 Kernel 中 Hub 静态地址，验证独立 Port/Contract/Adapter 与本机 fail-closed readiness

## 已实现并验证

- Kernel 新增调用方自有 `SystemServiceDirectory` Port 和 transport-independent endpoint DTO；
  domain/application 不依赖目录、HTTP 或 `eidolon_system`。
- `EidolondHttpServiceDirectory` 通过 UDS 或显式 loopback HTTP 消费版本化 Resolve API，严格校验
  consumed JSON Schema、endpoint identity、protocol 与 contract。
- Hub `DeviceAuthority` 每次低频 prerequisite 调用先 Resolve，再复用既有 owner-scoped Hub GET
  adapter；目录失败统一映射为 `AuthorityUnavailable`，SQLite mutation 不会提交。
- `hub.base_url` 已从 Kernel settings/composition 删除，不保留 fallback；Hub credential 只发送给
  Hub，不发送给机器级目录。
- `/health` 以同一 Resolve 结果报告 Device Mount 写 readiness；目录未 ready 时 authoritative
  SQLite 仍为 ready，但 mutation readiness 为 degraded。
- producer 与 consumer Schema 结构兼容性由 contract test 固定；组件测试使用真实
  `eidolon_system` HTTP app 生成响应，再由 Kernel adapter 消费，不共享运行时 DTO。
- 真实 Unix socket 组件测试验证 Kernel consumer 的 UDS 传输；`eidolond` 预绑定 listener、限制
  mode 后传递 fd，拒绝覆盖普通文件，避免 uvicorn 的默认 `0666`。
- Import Linter 与 AST 测试继续保证两个 package/process independence，以及 Kernel 各层 inward
  dependency。

## 验证命令与结果

```text
.venv/bin/pytest -q
135 passed

.venv/bin/pytest --cov=eidolon_kernel --cov=eidolon_system --cov-branch --cov-report=term-missing -q
135 passed; branch coverage 92.44% (gate 90%)

.venv/bin/ruff check eidolon_kernel eidolon_system tests scripts
All checks passed

.venv/bin/lint-imports
8 contracts kept, 0 broken

uv build --out-dir /private/tmp/eidolon-kernel-build-validation
source distribution and wheel built successfully
```

## 覆盖的失败面

- UDS/HTTP transport 不可连接；
- 404 endpoint 不存在、503 service 未 ready、其他非 200 status；
- JSON 非 object、未知字段、缺字段；
- service/endpoint identity、protocol 或 contract 漂移；
- 目录失败时禁止继续调用 Hub；
- 生产 composition 没有静态 Hub URL，目录缺失时 health 不虚报 write-ready。

## 剩余 blocker / 下一步门槛

1. 当前 dev supervisord 没有 Kernel program，`eidolond` 尚未接管 Kernel 自身 lifecycle。必须先有
   经过实际部署验证的 macOS program 与 Raspberry Pi systemd unit，不能猜测 target。
2. Companion authority 尚未纳入已验证的 system service manifest，因此仍保留精确静态配置；
   只有其 lifecycle target 与 readiness 已确认后才迁移。
3. Admin 仍是当前 supervisord 操作入口；在它迁移为 `eidolond` client 前，不允许两个 desired-state
   写入口同时管理 Hub。
4. 产品 Raspberry Pi 镜像仍需落实专用用户/组、UDS owner/group 与 systemd unit 安装验证；
   `0660` mode 已由代码强制，但组归属仍是部署事实。
5. 本阶段没有 dynamic register、lease/watch、多实例、跨 Host、通用代理或消息总线。
