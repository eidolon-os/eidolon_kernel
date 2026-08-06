# eidolon-kernel

Eidolon Kernel 是 Eidolon OS 的 **Sovereign Microkernel 控制面**。它只保存必须跨服务一致的全局 OS 事实；当前第一个纵向闭环是 **Device Mount**：把 Hub 已准入的 Device 挂入某个 Owner namespace，并可选择附着一个同 Owner Companion，同时提供权威状态、热读投影与有序审计。

当前项目是独立 Git 仓库，发布两个严格隔离的 Python package/进程：`eidolon_kernel`
继续承载 Sovereign Kernel；`eidolon_system` 提供独立的机器级 `eidolond` System Manager。
两者不相互 import、使用不同 SQLite authority，也不修改、导入或直连任何兄弟项目数据库。

## 角色与边界

| 组件 | 权威事实 / 职责 |
|---|---|
| Hub | Device onboarding、registry、`approved/revoked`、owner、manifest；Kernel 只消费其稳定 owner-scoped Device GET |
| Kernel | 全局 namespace、Device Mount、revision/CAS、幂等结果、authoritative state、audit |
| `eidolond` | 机器级系统服务 desired state、Host reconciliation、ready endpoint directory、系统操作审计；不是 Owner namespace |
| Companion authority (`eidolon_data`) | Companion 是否存在、是否 active、owner；Kernel 只消费稳定精确 Identity GET |
| Channel / Agent / Admin / Data | Kernel 的消费者或编排方，不把业务语义放入 Kernel |

Kernel 明确不实现 mDNS、MQTT、WSS、LiveKit、设备 command/state/event/media、Agent、模型、Memory、Persona，也不引入 NATS、Redis、gRPC、通用消息总线或共享黑板。Mount/Resolve 是低频 HTTP/JSON 控制面，不是媒体热路径。

## 当前状态

Device Mount 与可选 Companion Attachment 的 domain、application、SQLite、projection、Hub/Data authority consumer 和 HTTP V1 已形成完整闭环。Device 不需要 Companion 才能 Mount；Companion 也不需要物理 Device 才能存在。Hub 与 Data consumer 都使用精确 GET、严格 consumed JSON Schema 和独立服务凭证；生产 composition 不导入或直连兄弟项目数据库。

Kernel V1 只有一个安全主体：Owner。`owner_id` 是稳定、opaque 的 namespace principal，类似 OS UID；它不是账号资料、Persona、Companion 或业务对象。Kernel 不建立 token issuer、账号/profile authority 或 identity service。V1 只允许 loopback / trusted same-host ingress，由 `OwnerAuthorizer` 从受信本机安全上下文取得 Owner；request body/query 不能另行指定目标 Owner。Headless 一体机中的远端用户认证应终止在产品 ingress，Kernel 不重复验证终端用户 credential。只有 Kernel 需要直接暴露到不可信网络或跨 Host 时，才替换 authorizer adapter。详见 [ADR-0003](docs/adr/0003-v1-identity-and-authorization.md)。

## 收敛原则

Kernel 以 `Port + Contract + Adapter` 定义 System Service：领域 Port 表达稳定能力，wire contract 由事实拥有方发布，HTTP/SQLite 等 adapter 只处理传输和基础设施。协议不是领域边界，调用方也不通过通用 `ipc.call(service, method, payload)` 访问 Kernel。

`owner_id` 是 OS 中唯一、稳定、可持久化的安全与 namespace principal；token 只是可过期、轮换和撤销的 credential，不是 Owner identity。认证只发生在真实信任边界，不按进程数量重复堆叠 token：

- 产品 ingress 可以为同一 Owner 的不同登录会话签发多枚 credential；Kernel 只接收受信本机通道传递的 Owner context，执行 action/scope 授权和审计。
- V1 不建立独立 Actor principal。受信服务代表 Owner 调用时仍运行在该 Owner context；如果未来确有服务主体、委托链或提权审计需求，必须先以独立威胁模型和 ADR 证明，不能预先污染 Device Mount。
- Kernel 调用外部 authority 使用服务身份，不冒充 Owner credential。当前 Hub adapter 只能按 Hub 已发布的 management API 携带其要求的 credential；该凭证不进入 Domain、SQLite 或下游调用。
- 单机本地部署先接受明确的 trusted-local threat model。若以后需要防御同机不可信进程，再根据证据评估 Unix domain socket peer credential、mTLS 或 capability，而不是预建认证体系。

HTTP、gRPC、NATS、LiveKit 可以继续承载不同交互语义。当前只统一稳定 ID、principal/request context 和领域错误等必要语义，不统一 payload envelope，不实现 Binder daemon 或通用消息总线。仓库内独立 `eidolond` 只提供窄的机器级服务管理和 endpoint directory，不代理业务调用。依据和边界见 [ADR-0004](docs/adr/0004-system-service-contracts-without-binder.md) 与 [ADR-0007](docs/adr/0007-independent-system-manager-and-host-adapters.md)。

## 目录

```text
eidolon_kernel/        # Sovereign Kernel package；Device Mount bounded context
eidolon_system/        # 独立 eidolond package
├── domain/            # Service catalog、desired/observed state、不变量
├── application/       # Reconcile、Enable/Disable/Restart、Resolve
├── ports/             # Host supervisor、state store、directory、readiness
├── adapters/          # SQLite、typed projection、systemd/supervisord、HTTP probe
├── interfaces/http/   # /api/system/v1 HTTP/JSON
├── composition/       # eidolond 独立依赖组装与生命周期
├── contracts/         # 独立 normative JSON Schema 和显式 mapper
└── config.py          # Host/config 选择，不进入领域层
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

`eidolon_system` 使用相同的 inward-only 分层，但与 `eidolon_kernel` 整包 independence；架构测试
同时检查 package boundary 与 systemd/supervisord 字样不会进入 domain/application/ports。

## eidolond System Service 闭环

`eidolond` 解决的是单个 Eidolon 节点上的服务期望状态与逻辑寻址，不是 Nacos、Binder、通用
IPC、配置中心或服务网格。

1. 严格 manifest 声明稳定 `service_id`、显式 dependencies、不同 Host driver 的受限 target、
   原生 protocol endpoint、contract 和可选 readiness URL。
2. 首次加载把 `enabled_by_default` 写入独占 `eidolond.sqlite3`；之后 SQLite desired state 是唯一
   权威，manifest 不覆盖已有 revision。
3. Reconciler 按拓扑顺序启动 enabled service，反向停止 disabled service；required service 不可
   Disable，enabled dependent 会阻止 dependency 被 Disable。
4. Host 执行全部经过 `HostServiceSupervisor` Port：树莓派/Linux 使用 systemd adapter，当前
   macOS/dev 使用 supervisord adapter。Application 不 import 两者，也不直接 spawn 业务进程。
5. Host active 且 readiness 通过后，endpoint 才进入 typed in-memory directory；observed state
   不写入 SQLite，进程重启必须重新观察。
6. Enable/Disable/Restart 都要求幂等 request ID；desired mutation 要求 revision/CAS；每个已提交
   操作获得稳定递增 audit position。Restart 不改变 desired revision。

System service 是 machine scope，contract 中没有 `owner_id`。Owner 只属于 Kernel 用户
namespace；移动端或远端用户不得直接访问 `eidolond`。

### eidolond HTTP/JSON V1

| API | 作用 |
|---|---|
| `GET /api/system/v1/services` | 列出 desired 与 observed status |
| `GET /api/system/v1/services/{service_id}` | 获取一个系统服务状态 |
| `GET /api/system/v1/services/{service_id}/endpoints/{endpoint_id}` | 只 Resolve ready endpoint |
| `POST /api/system/v1/services/{service_id}/enable` | CAS 修改 desired state 并 reconcile |
| `POST /api/system/v1/services/{service_id}/disable` | CAS Disable；required/dependency fail closed |
| `POST /api/system/v1/services/{service_id}/restart` | 幂等 one-shot restart，不改变 desired revision |
| `GET /api/system/v1/audit/events` | 按稳定 position 读取机器级操作审计 |

V1 不允许服务主动 Register/Heartbeat。当前进程由 `eidolond` 管理，因此由控制器根据 Host 与
readiness 自动发布；动态端口、多实例、跨 Host 或外部 provider 出现后，才评估 lease/watch。

## Device Mount 流程

1. HTTP interface 先通过 normative JSON Schema 校验 wire request，再显式映射为 domain command。
2. `OwnerAuthorizer` 从本机安全上下文给出唯一 Owner scope；application 不信任 request body/query 自报 owner。
3. 相同 `request_id + fingerprint` 直接返回原 mutation result，不重新访问外部 authority；同一 request ID 的不同 payload 返回冲突。
4. 首次/重新挂载前，`DeviceAuthority` 只校验 Hub Device 存在、`approved`、owner 匹配；Mount 不访问 Companion authority。
5. application 构造下一 revision；SQLite 用 `BEGIN IMMEDIATE` 和 expected revision 做 CAS，在同一事务提交 mount、幂等结果和 audit event。
6. 提交成功后更新 typed in-memory projection；若增量更新异常，直接从 SQLite authority 重建。
7. Get/Resolve/List 走投影；audit 读取 SQLite。进程启动时投影只从当前 mount authority 重建。

Companion Attachment 是 Mount 上独立的可选状态迁移。Attach 时才通过 `CompanionAuthority` 校验 Companion 存在、`active` 且属于相同 Owner；Detach 只清除默认附着，不会 Unmount Device。Attachment 不是 Owner、ACL 或 Channel 当前会话路由。
跨项目 consumer 的最小边界与迁移门槛见 [ADR-0006](docs/adr/0006-optional-attachment-and-consumer-boundaries.md)。

### Revision 规则

- 首次挂载：`expected_revision=0`，产生 revision 1。
- active mount 不能被隐式覆盖；明确 remount 必须设置 `replace_existing=true` 并携带当前 revision。
- Unmount 必须携带当前 revision，产生 revision + 1 的 inactive tombstone，并原子结束
  可选 attachment；previous attachment 只保留在 audit。
- tombstone 后再次 Mount 必须携带 tombstone revision，产生新的 active revision；不需要 `replace_existing`。
- Attach/Detach 必须携带 active Mount 当前 revision，成功后产生 revision + 1；Attach 本身可明确替换已有 attachment。
- V1 每个 Device 最多一个 active mount 和一个可选默认 attachment；一个 Companion 可附着多个 Device。

`created_at` 表示当前 active mount incarnation 的建立时间；明确 remount 或 tombstone 后重挂会重置它。`updated_at`、request ID 和 fingerprint 表示最后一次状态迁移，记录始终保留同一 Owner。完整历史由 audit 保留。

## HTTP/JSON V1

所有 endpoint 都严格限定在当前 Owner namespace。V1 trusted-local 模式只接受一个安全上下文 header：

```http
X-Eidolon-Owner: <owner-id>
```

该 header 不是互联网认证凭证，只能由受信同机 ingress 设置。Mount/Unmount body 以及 Get/Resolve/List/Audit query 均不接受 `owner_id`，因此调用方不能在同一次请求中另选目标 Owner。

| API | 作用 |
|---|---|
| `POST /api/kernel/v1/device-mounts` | 首次 Mount、tombstone 后重挂或明确 Remount |
| `GET /api/kernel/v1/device-mounts/devices/{device_id}` | 当前 Owner 内 Get 记录，包含 inactive tombstone |
| `GET /api/kernel/v1/device-mounts/resolve/{device_id}` | 当前 Owner 内只 Resolve active mount |
| `GET /api/kernel/v1/device-mounts?companion_id=...` | 当前 Owner scope；可叠加 companion、cursor、limit、active filter |
| `POST /api/kernel/v1/device-mounts/devices/{device_id}/attachment` | 校验同 Owner active Companion 后 CAS Attach/Reattach |
| `POST /api/kernel/v1/device-mounts/devices/{device_id}/attachment/detach` | CAS Detach，Device 保持 mounted |
| `POST /api/kernel/v1/device-mounts/devices/{device_id}/unmount` | CAS Unmount |
| `GET /api/kernel/v1/audit/events?after_position=...` | 当前 Owner 内按稳定递增位置读取审计 |

Normative wire contract 位于 [`eidolon_kernel/contracts/schemas`](eidolon_kernel/contracts/schemas)。FastAPI binding 只负责运行时 normalization；interface 对输入输出再次执行 Draft 2020-12 Schema 校验。Wire DTO 与 Domain Entity 不共享类，由 mapper 显式转换。

Kernel 的 Hub adapter 只调用：

```text
GET {hub.base_url}/api/device-management/v1/owners/{owner_id}/devices/{device_id}
```

并透传 `EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN`。该值必须与 Hub 的
`EIDOLON_HUB_DEVICE_REGISTRY_READER_TOKEN` 相同，是只能执行精确 Device Get 的
opaque capability；它不是 Owner token，也不是不可续期的静态 JWT。Kernel 不消费
Approval、Revocation、Events、Enrollment 或 Provider API。

Kernel 的 Companion adapter 只调用：

```text
GET {companion_authority.base_url}/api/companion-authority/v1/companions/{companion_id}
```

并透传 `EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN`。该值必须与 Data Authority 的
`EIDOLON_DATA_COMPANION_AUTHORITY_TOKEN` 相同。两个 consumer 都只接受各自固定的
consumed JSON Schema，wire DTO 再显式映射为 Kernel domain identity。

生产 composition 会立即并周期性重验所有 active Mount。Device revoked/缺失/Owner
不匹配时，Kernel 以当前 revision 做 CAS，写入 inactive tombstone；只有当前 Mount
带 attachment 时才重验 Companion，Companion inactive/缺失只 CAS Detach，Device 仍在
Owner namespace。两种变化都写审计；网络、认证、5xx 或契约故障只延后本轮。

## 存储

默认 authority 是项目内 `var/eidolon-kernel.sqlite3`，使用 WAL 和 `<database>.lock` 进程锁：

| 表 | 内容 |
|---|---|
| `kernel_device_mounts` | 每个 Device 的当前 active mount 或 inactive tombstone |
| `kernel_requests` | 全局 request ID、operation、fingerprint、稳定 mutation outcome |
| `kernel_audit_events` | `AUTOINCREMENT` position 的不可变状态迁移审计 |
| `kernel_schema_meta` | 精确 schema version |

SQLite 是唯一权威；projection 不是第二事实源。当前 schema version 为 3。空库按当前 schema 创建，旧库、部分库或未知表直接拒绝。开发阶段不提供 migration 或兼容。

## 不变量

1. Device、Owner、Companion 使用稳定 ID，不从 IP、Channel ID、Room 或 transport 推导。
2. Kernel 不批准设备，也不创建/激活 Companion；Mount 只依赖 Hub Device admission，Attach 才引用 Companion authority。
3. active DeviceMount 必须满足 Device approved + owner 匹配；可选 attachment 若存在，Companion 必须 active 且与 Mount 属于同一 Owner namespace。
4. 一个 Device 最多一个 active mount 和一个默认 attachment；Companion 可存在零个 Device，也可附着多个 Device。
5. 所有 mutation 都要求全局幂等 request ID、canonical fingerprint 和 revision/CAS。
6. SQLite commit 先于 projection；DB 写失败不得更新内存，projection 必须可重建。
7. 每个已提交 mutation 恰有一个 audit position；重放不产生新 position。
8. Device ID 可以全局稳定，但 Get/Resolve/List/Mutation/Audit 都必须由 Owner security context 限定；跨 Owner 一律 fail closed，remount 不能转移 Owner。
9. HTTP/router 不直接操作 SQLite；domain/application 不导入框架、网络或存储实现。

## 配置与运行

```bash
uv sync --all-groups
export EIDOLON_KERNEL_HUB_MANAGEMENT_TOKEN='<same opaque token as Hub registry reader>'
export EIDOLON_KERNEL_COMPANION_AUTHORITY_TOKEN='<same opaque token as Data authority>'
uv run uvicorn eidolon_kernel.main:create_app --factory --host 127.0.0.1 --port 8083
```

V1 必须绑定 loopback 或置于已经完成身份认证的同机 ingress 后；不得直接监听不可信网络。Companion Authority 默认由 `127.0.0.1:8084` 提供。

独立运行 System Manager：

```bash
# 默认 config/eidolond.yaml 是当前 macOS/dev supervisord 过渡配置。
uv run eidolond

# 树莓派/Linux 产品镜像提供自己的 /etc/eidolon/eidolond.yaml：
EIDOLON_SYSTEM_SETTINGS_YAML=/etc/eidolon/eidolond.yaml uv run eidolond
```

`config/eidolond.systemd.example.yaml` 展示 systemd adapter 与 `/run/eidolon/system.sock` 配置。
Host init 必须启动并拉起 `eidolond`；只有 `eidolond` 应拥有其他 Eidolon unit 的 desired state。
产品部署应使用 UDS 文件 owner/group/mode 限制调用者。当前 macOS/dev manifest 只包含代码已确认
target、health 与 authority contract 的 Hub；systemd manifest 是等待树莓派镜像安装/验证 unit
name 的 deployment example。它不会猜测 Agent/Channel/Memory 的完整依赖关系，也尚未
接管 Kernel 自身的 dev lifecycle。在 Admin 仍可修改 supervisord enabled symlink 的过渡期，
不要同时用两处入口修改 Hub enablement；正式切换前必须先把 Admin 降为 `eidolond` client。

## 验证

```bash
uv run ruff check eidolon_kernel eidolon_system tests scripts
uv run lint-imports
uv run pytest -q
uv run pytest --cov=eidolon_kernel --cov=eidolon_system --cov-report=term-missing -q
```

测试分为 unit、contract、component、functional、E2E 和 architecture。最新结果见
[Optional Attachment 与消费者边界收敛测试报告](docs/testing/reports/2026-08-05-optional-attachment-convergence.md)。

## 后续演进门槛

- Companion Authority producer/consumer schema 必须保持兼容，并由跨项目契约门禁检测漂移。
- Kernel 不拥有 Owner profile/account；未来即使需要读取 Owner lifecycle，也必须由事实拥有方先发布窄且稳定的 authority contract，不能直连兄弟 DB 或在 Kernel 发明用户资料 API。
- 保持 Kernel local-only 时，trusted-local authorizer 是明确部署假设而不是产品化 blocker；若要直接接入不可信网络或跨 Host，必须先定义可验证 principal 和 ingress-to-Kernel 信任通道，再替换 adapter。不得把 header hints 包装成“认证”。
- 动态服务自行注册、Lease/Watch、多实例或跨 Host registry 必须先出现真实部署事实；当前 controller-derived local directory 不因此升级为 Binder-like runtime。
- 只有出现跨 Host 服务迁移、能力句柄或通用调用代理等实际需求，才另行评估 Binder-like IPC runtime；“已经用了多种协议”本身不是引入依据。
- 只有 HTTP/JSON 的测量结果无法满足控制面 SLA 时，才评估 gRPC；媒体热路径永远不经 Device Mount API。
