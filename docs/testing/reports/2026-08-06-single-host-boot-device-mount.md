# 单 Host 启动与 Device Mount 进程 E2E 报告

- 日期：2026-08-06
- 范围：eidolond + Hub + Kernel 的单机生命周期、directory 与 Device Mount
- 部署载体：macOS 隔离 supervisord 真实进程；Raspberry Pi 5 / Debian 13 / systemd 257 真机

## 真实进程闭环

`tests/e2e/test_system_boot_device_mount_process_e2e.py` 使用测试专属短临时目录、端口、UDS 与
SQLite，不访问正式数据库或现有 daemon。实际启动 sibling Hub uvicorn、supervisord、eidolond 和
Kernel uvicorn，而不是 ASGI in-process transport。

已验证：

- STOPPED Hub/Kernel 由 eidolond 冷启动并在 readiness 后发布 endpoint；
- Hub 真实 Enrollment/Approval 后 Kernel 通过 eidolond directory 完成 Mount；
- eidolond API 受管重启 Kernel，Mount authority 在进程重启后仍存在；
- SIGSTOP Hub 后 readiness 失败、Hub endpoint Resolve 503、Kernel 写 health degraded；
- Hub 故障期间已有 Mount 仍可读，新 Mount 503 且不提交；
- SIGCONT 后 endpoint 恢复，同一失败 request ID 可安全重试成功；
- eidolond 重启后 desired state 和稳定 audit position 从独占 SQLite 恢复。

首轮 E2E 揭示并修复 supervisord 冷启动缺陷：`status` 对 STOPPED 返回码 3。adapter 现在只在
输出目标匹配且状态属于 supervisor 有限状态集合时接受该观察；错误输出仍 fail closed。

## systemd 部署门禁

静态测试验证：

- 只有 eidolond unit 可由 `multi-user.target` enable；
- Hub/Kernel unit 无 `[Install]`，但保留 systemd crash restart；
- 三个 unit 以 `eidolon` 用户运行、无 capabilities、不经 shell；
- Polkit rule 锁定 subject unit、NoNewPrivileges、两个 target 和三个 verb；
- systemd manifest 的 target、真实 `/health` 路径、Kernel soft dependency 与 unit 一致。

## Raspberry Pi 真机闭环

使用专用 `eidolon` 非登录账号，把 Hub/Kernel 安装到独立 release；`/srv/eidolon/current` 只新增
两个 symlink，既有 `eidolon_admin` 与 `eidolon-bootstrapd` 未修改。只有 `eidolond.service` 被
enable，Hub/Kernel 由 eidolond desired state 拉起。

首轮 Hub 启动由日志确认失败于 `ifaddr.get_adapters()`：unit 缺少 Linux interface discovery 所需
的 `AF_NETLINK`。新增失败测试后，仅为 Hub unit 放行该 address family。修复后验证：

- Bootstrap、eidolond、Hub、Kernel 全部 `active/running`；最终 `NRestarts=0`；
- eidolond health `ready`，directory 中 Hub/Kernel 均为 `runtime_state=ready`；
- UDS 为 `srw-rw---- eidolon:eidolon`，Hub/Kernel 仅监听 `127.0.0.1:8082/8083`；
- Hub、Kernel、eidolond SQLite 均存在且 `PRAGMA integrity_check=ok`；
- Hub Enrollment/Approval 得到 `approved`、Owner-scoped Device；
- Kernel 创建 revision 1、active、无 Companion Attachment 的 Mount，audit position 为 1；
- 两次由 eidolond 发起的 Kernel restart 均进入递增系统审计；Mount 在重启后仍为 revision 1。

## 已执行命令

```text
uv run pytest tests/e2e/test_system_boot_device_mount_process_e2e.py -q -s
1 passed in 9.62s

uv run pytest tests/system/test_systemd_deployment.py tests/system/test_manifest_contract.py -q
11 passed

uv run pytest tests/system/test_host_adapters.py -q
5 passed

uv run pytest --cov=eidolon_kernel --cov=eidolon_system --cov-report=term-missing -q
150 passed in 16.87s; branch coverage 92.71% (gate 90%)

uv run ruff check eidolon_kernel eidolon_system tests scripts
All checks passed

uv run lint-imports
8 contracts kept, 0 broken

uv run ruff format --check <本轮触及的 5 个 Python 文件>
5 files already formatted

uv build
sdist + wheel built successfully
```

## 剩余边界

- 未把 Agent、Channel、Memory、Data 纳入 manifest；
- 未修改 Admin 默认 supervisord 配置，避免在迁移完成前形成双 desired-state authority。
