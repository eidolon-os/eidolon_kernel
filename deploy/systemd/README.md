# Raspberry Pi / Linux systemd deployment

这些文件是正式 Pi product-image/release 输入，不是 macOS supervisord 配置。固定运行图是：

```text
eidolon-bootstrapd -> Local API / Admin

eidolond (唯一 system-service desired-state authority)
  ├─ NATS -> Memory Supervisor -> Memory Discovery -> Agent -> Channel
  ├─ LiveKit -> Hub / Channel
  ├─ Data -> Data Workspace / Agent / Channel
  ├─ Hub
  └─ Kernel -> Channel
```

Systemd 拥有 PID、cgroup、signals 和 crash restart；`eidolond.sqlite3` 拥有 NATS、LiveKit、Data、
Data Workspace、Hub、Kernel、Memory、Agent、Channel 是否应运行。子 unit 没有 `WantedBy=`，不得单独
enable。Bootstrap、unit applier socket、eidolond、Local API、Admin 是 first-install 直接 enable 的
5 个顶层 unit。

Image/first-install 边界必须：

1. 创建非登录 `eidolon` 与 `eidolon-bootstrap` 身份及固定 state/log 目录。
2. 安装并以 `systemd-analyze verify` 检查 16 个 product unit；安装固定 Polkit（仅 Admin 的
   NetworkManager 规则）与 Avahi 资产。
3. 安装 `/etc/eidolon/eidolond.yaml`、Kernel/Hub/service manifest，以及 Agent、Channel、Memory 的
   settings YAML；settings 是 `root:eidolon 0640`。
4. 安装 11 个固定私密前置文件。env/Host identity 是 `0600`，不把值放进 unit、descriptor、snapshot
   或 receipt。
5. 从 Data 当前提交的 `0001_system_data_v2` 建立全新 baseline；绝不恢复旧迁移或旧
   `eidolon.sqlite3`。
6. 在 `/opt/eidolon/current/` 提供 7 个精确 release link；SDK 是构建输入，不是 runtime service。
7. 由固定 foundation profile 提供 `/usr/local/bin/nats-server`、`livekit-server`，以及 BlueZ、
   NetworkManager、Avahi、FFmpeg、uv、Node 和编译/运行库。
8. 仅 enable Bootstrap、`eidolon-unit-applier.socket`、eidolond、Local API、Admin；其余服务由
   eidolond reconciliation 拉起。

eidolond 以 `eidolon` 身份、`NoNewPrivileges` 运行，自己起不了任何 unit。start/stop/restart 由
`eidolon-unit-applier.service`（root，socket 激活）代为执行：它按 `SO_PEERPIDFD`/`SO_PEERCRED` 取
调用方 pid，再从其 cgroup 解析出调用方所属 unit，必须是 `eidolond.service`；可操作的 unit 集合在启动
时从 `/etc/eidolon/system-services.yaml` 推导，不是另抄一份清单。enable/disable/daemon-reload 不在
其中——那是 Ops 以 root 做的安装期动作。每一次放行与拒绝都写 journal，拒绝会指名是哪一个条件不成立。

这取代了原先的 polkit 规则：那条规则拒绝时在这块板子上什么都不写，于是"每个服务都起不来"没有任何可读
的原因。部署工具不读取任何兄弟数据库。

完整 release、readiness 与 rollback 语义见
[`docs/operations/target-release.md`](../../docs/operations/target-release.md)。Mac 侧从新 Pi provision 到
App host gate 的唯一入口在独立 `eidolon_ops`。真实 Pi 的完整 16-unit 验收仍需明确授权。
