# ADR-0010: 单 Host 启动、systemd 机制与应用栈 readiness

- 状态：Accepted，部署资产与本机进程 E2E 已实现
- 日期：2026-08-06

## Context

Android + Raspberry Pi 真机测试已经完成 `BLE Setup → Wi-Fi → Controller 认领`，但最终
`workspace_state=absent`。这证明 Host onboarding readiness 与 Eidolon 应用栈 readiness 是两类
事实。Bootstrap 由 Admin-owned `eidolon-bootstrapd` 早启并持续可用；Kernel/eidolond 不应复制
BLE、Wi-Fi、Controller 或恢复语义，也不能把 `claimed + connected` 宣称为 Device Mount ready。

代码侧已有 `eidolond` desired state、systemd/supervisord Host adapter 和 ready endpoint directory，
但原实现缺少三个部署事实：实际 unit、非 root 管理 unit 的最小权限、真实冷启动进程闭环。真实
supervisord E2E 还发现 `supervisorctl status` 对合法 STOPPED program 返回 3，旧 adapter 因而无法
从冷启动启动任何 service。

## Decision

### Host init 只拥有 eidolond

产品 systemd enable `eidolond.service`。`eidolon-hub.service` 与 `eidolon-kernel.service` 不提供
`WantedBy=`，避免 systemd enablement 与 `eidolond.sqlite3` 同时成为 desired-state authority。
systemd 继续拥有 PID、cgroup、信号和 crash restart；eidolond 只决定 desired state 并调用固定
unit 的 start/stop/restart。

### 非 root、最小 systemd 权限

三个进程均使用非登录 `eidolon` 用户，设置 `NoNewPrivileges=yes`、空 capability bounding set 和
只读系统保护。Polkit rule 同时校验：

- action 必须是 `org.freedesktop.systemd1.manage-units`；
- subject 必须是 `eidolon` 且来自 `eidolond.service`；
- systemd 必须确认 subject 的 `NoNewPrivileges`；
- target 只能是 Hub/Kernel 两个 unit；
- verb 只能是 start/stop/restart。

规则不授权 enable/disable unit file、daemon reload 或任意 unit 管理。服务 secret 只放在 root-owned
`/etc/eidolon/*.env`，不进入 YAML、unit、Domain 或 SQLite。

### Hub 是 Kernel 写入的软依赖

Kernel 不声明对 Hub 的硬 service dependency。Hub 不 ready 时，Kernel 仍应启动、恢复自己的
SQLite/projection 并服务已有 Owner-scoped Mount 读取；新 Mount 和 prerequisite reconciliation 通过
directory fail closed。Kernel `/health` 区分 authoritative store 与 write availability。

因此 eidolond 的 Kernel process readiness 可以保持 ready，而 Kernel capability health 返回
degraded。这不是状态矛盾：前者说明进程可服务，后者说明特定写能力缺少外部 authority。

### 平台边界

- Raspberry Pi/Linux：使用本仓 `deploy/systemd` unit、Polkit rule 与 systemd YAML profile；
- macOS/dev：继续使用 supervisord Host adapter。同一代码的隔离真实进程 E2E 已通过；在 Admin
  默认配置安装 Kernel program 前，不宣称开发机迁移完成；
- Mobile、Local API、Web Admin 不直连 eidolond。未来由 authenticated product ingress 聚合
  Host onboarding state 与 stack state，但事实仍分别拥有。

## Verified behavior

真实 E2E 使用独立临时 SQLite、UDS、端口和 supervisord，启动真实 Hub、eidolond 与 Kernel：

1. eidolond 从 STOPPED 冷启动 Hub/Kernel；
2. Hub 完成 Enrollment/Approval，Kernel 完成 Device Mount；
3. eidolond 重启 Kernel 后 Mount 从 Kernel SQLite 恢复；
4. SIGSTOP Hub 后 endpoint 被撤销，Kernel 已有读成功、新 Mount 返回 503；
5. Hub 恢复后同一 request ID 的 Mount 成功；
6. eidolond 重启后 desired state 与系统审计位置恢复。

STOPPED 返回码处理现按目标名和 supervisor 有限状态集合解析；`no such process` 等非状态输出仍
fail closed。

Raspberry Pi 5 / Debian 13 / systemd 257 真机还验证了：

1. 既有 `eidolon-bootstrapd` 保持 active，Admin release 未被覆盖；
2. 只有 `eidolond.service` enable，Hub/Kernel 由 eidolond 通过受限 Polkit 权限冷启动；
3. UDS 为 `0660 eidolon:eidolon`，8082/8083 只监听 loopback；
4. Hub Enrollment/Approval 后 Kernel 创建无 Companion Attachment 的 Owner-scoped Mount；
5. eidolond 受管重启 Kernel 后，revision 1 Mount 从独占 SQLite 恢复；
6. eidolond、Hub、Kernel 三个 SQLite `integrity_check=ok`。

首轮真机启动还发现 Hub mDNS 的 Linux interface discovery 需要 `AF_NETLINK`；原 unit 的 address
family 白名单阻断了 ifaddr。部署测试先复现失败，Hub unit 随后只增加 `AF_NETLINK`，Kernel 与
eidolond 的白名单没有放宽。

## Consequences and blockers

- 不新增 NATS、Binder、动态 registration/lease/watch、launchd adapter 或完整 stack manifest。
- Agent、Channel、Memory、Data 尚未纳入 system manifest；必须逐项确认真实 target、hard/soft
  dependency 与 readiness 后再加入。
- Admin 仍拥有现有 supervisord 管理入口。迁移期间不能让 Admin 与 eidolond 同时修改同一 service
  desired state；后续应让 Admin 只消费 System Manager API。
