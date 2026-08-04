# eidolon-kernel

Eidolon Kernel 是 Eidolon OS 的 **Sovereign Microkernel 控制面**。它只保存必须跨服务一致的全局 OS 事实；当前第一个纵向闭环是 **Device Mount**：把 Hub 已准入的 Device 挂入某个 Owner 的 Companion namespace，并提供权威状态、热读投影与有序审计。

当前项目是独立 Git 仓库、独立 Python package 和独占 SQLite authority。它不修改、导入或直连任何兄弟项目数据库。

## 角色与边界

| 组件 | 权威事实 / 职责 |
|---|---|
| Hub | Device onboarding、registry、`approved/revoked`、owner、manifest；Kernel 只消费其稳定 owner-scoped Device GET |
| Kernel | 全局 namespace、Device Mount、revision/CAS、幂等结果、authoritative state、audit |
| Companion authority | Companion 是否存在、是否 active、owner；当前尚缺稳定跨项目契约 |
| Channel / Agent / Admin / Data | Kernel 的消费者或编排方，不把业务语义放入 Kernel |

Kernel 明确不实现 mDNS、MQTT、WSS、LiveKit、设备 command/state/event/media、Agent、模型、Memory、Persona，也不引入 NATS、Redis、gRPC、通用消息总线或共享黑板。Mount/Resolve 是低频 HTTP/JSON 控制面，不是媒体热路径。

## 当前状态

Device Mount 的 domain、application、SQLite、projection、Hub consumer 和 HTTP V1 已形成完整、可注入验证的闭环。生产 composition 对 Companion 校验 **fail closed**：现有代码没有独立、版本化、带明确认证和 lifecycle enum 的 Companion authority contract，因此写请求返回 `503`，直到该契约由其事实拥有方发布。详见 [ADR-0002](docs/adr/0002-companion-authority-blocker.md)。测试 fake 只存在于测试目录，不进入生产组合。

Kernel 不建立新的 JWT 或 identity service。V1 只允许 loopback / trusted same-host ingress，把显式 actor hints 交给 `ActorAuthorizer` Port；这些 hints 用于 owner scope 和审计归因，不是登录凭证。Headless 一体机中的远端用户认证应终止在产品 ingress，Kernel 不重复验证小程序、LiveKit 或 Agent runtime token。只有 Kernel 需要直接暴露到不可信网络或跨 Host 时，才替换 authorizer adapter。详见 [ADR-0003](docs/adr/0003-v1-identity-and-authorization.md)。

## 收敛原则

Kernel 以 `Port + Contract + Adapter` 定义 System Service：领域 Port 表达稳定能力，wire contract 由事实拥有方发布，HTTP/SQLite 等 adapter 只处理传输和基础设施。协议不是领域边界，调用方也不通过通用 `ipc.call(service, method, payload)` 访问 Kernel。

认证只发生在真实信任边界，不按进程数量重复堆叠 JWT：

- 小程序、Web 或 Admin 的用户身份由产品 ingress 验证，再经受信本机通道传递最小 principal；Kernel 只做自身 action/scope 授权和审计。
- Kernel 调用 Hub 时使用 Hub 当前 management API 要求的服务凭证；这不是 Kernel identity，也不能传播成全局万能 token。
- LiveKit、Agent runtime 等专用 token 留在所属链路，Kernel 不解析、不签发，也不复制其 shared secret。
- 单机本地部署先接受明确的 trusted-local threat model。若以后需要防御同机不可信进程，再根据证据评估 Unix domain socket peer credential、mTLS 或 capability，而不是预建认证体系。

HTTP、gRPC、NATS、LiveKit 可以继续承载不同交互语义。当前只统一稳定 ID、principal/request context 和领域错误等必要语义，不统一 payload envelope，不实现 Binder daemon、动态 Service Manager 或通用消息总线。依据和引入门槛见 [ADR-0004](docs/adr/0004-system-service-contracts-without-binder.md)。

## 目录

```text
eidolon_kernel/
├── domain/          # DeviceMount、Actor、Command、不变量和稳定错误
├── application/     # Mount/Unmount use case、Get/Resolve/List/Audit query
├── ports/           # Hub、Companion、Authorizer、Store、Projection、Clock
├── adapters/        # SQLite、内存投影、Hub HTTP、fail-closed Companion、本机信任
├── interfaces/http/ # /api/kernel/v1 HTTP/JSON
├── composition/     # 唯一依赖组装与生命周期管理点
├── contracts/       # Normative JSON Schema、严格 wire binding、显式 mapper
└── config.py        # 严格 local-only 配置
```

依赖方向由架构测试和 `lint-imports` 阻塞：

```text
domain
ports       -> domain
application -> ports + domain
contracts   -> ports + domain
adapters    -> contracts + ports + domain
interfaces  -> contracts + application + ports + domain
composition -> all layers
main        -> composition
```

## Device Mount 流程

1. HTTP interface 先通过 normative JSON Schema 校验 wire request，再显式映射为 domain command。
2. `ActorAuthorizer` 给出 actor 和 owner scope；application 不信任 HTTP header 本身。
3. 相同 `request_id + fingerprint` 直接返回原 mutation result，不重新访问外部 authority；同一 request ID 的不同 payload 返回冲突。
4. 首次/重新挂载前，`DeviceAuthority` 校验 Hub Device 存在、`approved`、owner 匹配；`CompanionAuthority` 校验 Companion 存在、`active`、owner 匹配。
5. application 构造下一 revision；SQLite 用 `BEGIN IMMEDIATE` 和 expected revision 做 CAS，在同一事务提交 mount、幂等结果和 audit event。
6. 提交成功后更新 typed in-memory projection；若增量更新异常，直接从 SQLite authority 重建。
7. Get/Resolve/List 走投影；audit 读取 SQLite。进程启动时投影只从当前 mount authority 重建。

### Revision 规则

- 首次挂载：`expected_revision=0`，产生 revision 1。
- active mount 不能被隐式覆盖；明确 remount 必须设置 `replace_existing=true` 并携带当前 revision。
- Unmount 必须携带当前 revision，产生 revision + 1 的 inactive tombstone。
- tombstone 后再次 Mount 必须携带 tombstone revision，产生新的 active revision；不需要 `replace_existing`。
- V1 每个 Device 最多一个 active mount；一个 Companion 可挂多个 Device。

`created_at` 表示当前 active mount incarnation 的建立时间；明确 remount 或 tombstone 后重挂会重置它。`updated_at`、actor、request ID 和 fingerprint 表示最后一次状态迁移。完整历史由 audit 保留。

## HTTP/JSON V1

所有 owner-scoped endpoint 在 V1 trusted-local 模式下要求：

```http
X-Eidolon-Actor: <actor-id>
X-Eidolon-Owner: <owner-id>
```

| API | 作用 |
|---|---|
| `POST /api/kernel/v1/device-mounts` | 首次 Mount、tombstone 后重挂或明确 Remount |
| `GET /api/kernel/v1/device-mounts/devices/{device_id}?owner_id=...` | Get 当前记录，包含 inactive tombstone |
| `GET /api/kernel/v1/device-mounts/resolve/{device_id}?owner_id=...` | 只 Resolve active mount |
| `GET /api/kernel/v1/device-mounts?owner_id=...&companion_id=...` | owner scope；可叠加 companion scope、cursor、limit、active filter |
| `POST /api/kernel/v1/device-mounts/devices/{device_id}/unmount` | CAS Unmount |
| `GET /api/kernel/v1/audit/events?owner_id=...&after_position=...` | 按稳定递增位置读取审计 |

Normative wire contract 位于 [`eidolon_kernel/contracts/schemas`](eidolon_kernel/contracts/schemas)。FastAPI binding 只负责运行时 normalization；interface 对输入输出再次执行 Draft 2020-12 Schema 校验。Wire DTO 与 Domain Entity 不共享类，由 mapper 显式转换。

Kernel 的 Hub adapter 只调用：

```text
GET {hub.base_url}/api/device-management/v1/owners/{owner_id}/devices/{device_id}
```

并透传 `EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN`。它不签发 Hub JWT，不消费 Approval、Revocation、Events、Enrollment 或 Provider API。

## 存储

默认 authority 是项目内 `var/eidolon-kernel.sqlite3`，使用 WAL 和 `<database>.lock` 进程锁：

| 表 | 内容 |
|---|---|
| `kernel_device_mounts` | 每个 Device 的当前 active mount 或 inactive tombstone |
| `kernel_requests` | 全局 request ID、operation、fingerprint、稳定 mutation outcome |
| `kernel_audit_events` | `AUTOINCREMENT` position 的不可变状态迁移审计 |
| `kernel_schema_meta` | 精确 schema version |

SQLite 是唯一权威；projection 不是第二事实源。空库按当前 schema 创建，旧库、部分库或未知表直接拒绝。开发阶段不提供 migration 或兼容。

## 不变量

1. Device、Owner、Companion 使用稳定 ID，不从 IP、Channel ID、Room 或 transport 推导。
2. Kernel 不批准设备，也不创建/激活 Companion；Mount 只引用两个外部 authority 已确认的事实。
3. active DeviceMount 必须同时满足 Device approved + owner 匹配及 Companion active + owner 匹配。
4. 一个 Device 最多一个 active mount；Companion 可挂多个 Device。
5. 所有 mutation 都要求全局幂等 request ID、canonical fingerprint 和 revision/CAS。
6. SQLite commit 先于 projection；DB 写失败不得更新内存，projection 必须可重建。
7. 每个已提交 mutation 恰有一个 audit position；重放不产生新 position。
8. HTTP/router 不直接操作 SQLite；domain/application 不导入框架、网络或存储实现。

## 配置与运行

```bash
uv sync --all-groups
export EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN='<Hub-issued token>'
uv run uvicorn eidolon_kernel.main:create_app --factory --host 127.0.0.1 --port 8083
```

V1 必须绑定 loopback 或置于已经完成身份认证的同机 ingress 后；不得直接监听不可信网络。当前 production health 是 `degraded`，并明确报告 `companion-authority-contract` blocker。

## 验证

```bash
uv run ruff check eidolon_kernel tests scripts
uv run lint-imports
uv run pytest -q
uv run pytest --cov=eidolon_kernel --cov-report=term-missing -q
```

测试分为 unit、contract、component、functional、E2E 和 architecture。最新结果见 [Device Mount 测试报告](docs/testing/reports/2026-08-04-device-mount.md)。

## 后续演进门槛

- 先由 Companion 事实拥有方发布 versioned read contract、lifecycle enum、认证方式和兼容策略，再增加生产 Companion adapter。
- 保持 Kernel local-only 时，trusted-local authorizer 是明确部署假设而不是产品化 blocker；若要直接接入不可信网络或跨 Host，必须先定义可验证 principal 和 ingress-to-Kernel 信任通道，再替换 adapter。不得把 header hints 包装成“认证”。
- Capability/Lease、Service Registry 或其他 namespace 模块必须先证明其事实确需跨服务全局权威，并更新 ADR/架构测试。
- 只有出现动态服务发现、跨 Host 服务迁移、能力句柄、服务死亡通知等实际需求，才评估 Binder-like IPC runtime；“已经用了多种协议”本身不是引入依据。
- 只有 HTTP/JSON 的测量结果无法满足控制面 SLA 时，才评估 gRPC；媒体热路径永远不经 Device Mount API。
