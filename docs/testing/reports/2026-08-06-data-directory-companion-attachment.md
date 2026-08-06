# Data Directory 与 Companion Attachment 验证报告

- 日期：2026-08-06
- 范围：Kernel → eidolond directory → Data Companion Authority；Data/Hub 独立 capability health
- 部署状态：代码、本地真实进程 E2E 与 Raspberry Pi 持久化激活/整机重启已完成

## 实现边界

- 删除 Kernel 的 Data 静态 `base_url`，生产 composition 每次通过
  `SystemServiceDirectory` Port Resolve `data/companion-authority.http`。
- 固定 `protocol=http` 和 Data producer Schema 的规范 `$id`；directory/identity/contract 漂移均
  fail closed，且没有静态 fallback。
- 实际 HTTP GET 继续由既有 Data V2 consumer adapter、Kernel-owned consumed Schema、wire DTO 和
  mapper 完成；Kernel 不 import Data package、ORM 或数据库。
- Data、Hub 无 hard dependency。Kernel health 分别报告 Device Mount 与 Companion Attachment
  write availability；一方 unavailable 不阻断另一方或已有 Mount 热读。
- 新增非 root `eidolon-data.service`、精确 Polkit target 和 systemd manifest endpoint。Data V2 DB
  由 Data Alembic 部署步骤初始化，Kernel 永不创建或迁移它。
- 未修改 `eidolon_data`、`eidolon_channel` 或其他兄弟项目；Channel audio pipeline 不经过本变更。

## 自动化验证

真实 Data/Kernel 进程 E2E 从空库运行 Data V2 baseline migration，启动 Data Authority、fake typed
directory 与真实 Kernel，验证：Mount、12 路同 request 并发 Attach 幂等、Owner mismatch、missing
Companion、Data 停机返回 503 且不提交。另一个真实 Hub/eidolond/Kernel E2E 证明 Data 缺失时
Device Mount 能力保持可用，Kernel 只报告 Companion Attachment degraded。

最终门禁：

```text
.venv/bin/pytest -q --cov=eidolon_kernel --cov=eidolon_system --cov-branch --cov-report=term-missing
154 passed in 17.80s; branch coverage 92.66% (gate 90%)

.venv/bin/ruff check eidolon_kernel eidolon_system tests scripts
All checks passed

.venv/bin/ruff format --check <本轮触及的 12 个 Python 文件>
12 files already formatted

.venv/bin/lint-imports
8 contracts kept, 0 broken

uv build --out-dir /private/tmp/eidolon-kernel-m2c-build-final
sdist + wheel built successfully

git diff --check
passed
```

覆盖率第一次在受限沙箱内运行时，两项真实进程 E2E 因 loopback bind 被 OS 拒绝；coverage 数值仍
达到 92.30%，但该次不计为通过。随后以本机测试权限原样重跑，两个 E2E 均真实执行且得到上述
154 passed、无 skip 的最终结果。

## Raspberry Pi 激活与实测

新 release 已包含 Kernel、Data 与经审计的 SDK build；两个 venv 安装/import 成功。Data V2 baseline
已在独占 `/var/lib/eidolon/eidolon-system.sqlite3` 执行并通过 `validate_schema()`，Data secret 文件已
以 root-owned `0600` 准备。取得准确范围的显式授权后：

- Kernel commit `090c541989b97879dcc703d7caf11d240adc48b5` 以 Git archive 创建独立 release
  `/srv/eidolon/releases/20260806-m2c-090c541/eidolon_kernel`，关键文件 SHA 与 commit 相同；
- Data 使用从其 clean HEAD 构建的
  `/srv/eidolon/releases/20260806-m2c-632eeedw/eidolon_data`；两个 current symlink 原子切换；
- 安装 `eidolon-data.service`，更新 manifest、Kernel config 与只增加精确 Data target 的 Polkit rule；
  旧配置备份到 `/etc/eidolon/backups/20260806-m2c-090c541`，旧 release 未删除；
- 只有 Bootstrap/eidolond enabled，Data/Hub/Kernel 仍是 static；五个服务全部 active，三个 system
  service 均由 eidolond 发布为 ready；8082/8083/8084 只监听 loopback，UDS 为 `0660 eidolon:eidolon`；
- 通过 Data `DataStore` application service 创建同 Owner active Companion，再经 Kernel HTTP 完成
  Attach：Mount 从 revision 1 变为 revision 2，audit position 为 2；同 request 重放在重启前后均
  返回 `replayed=true` 且不新增审计；
- SIGSTOP Data 后 Kernel 只报告 `companion_attachment_write_available=false`，同时
  `device_mount_write_available=true`、Hub endpoint 与已有 revision 2 Mount 热读保持可用；SIGCONT
  后自动恢复，两服务 `NRestarts=0`；
- 整机 boot ID 从 `488449d3-0489-445b-aa30-52bb1e753ed7` 变为
  `165ca004-df5c-4d44-a466-8fa54ecc85d5`。冷启动后 revision 2 Attachment 与幂等结果均恢复；
  eidolond、Hub、Kernel、Data 四个 SQLite 全部 `integrity_check=ok`、无外键违规，四个 Eidolon
  unit `NRestarts=0`，本次 boot 的 Eidolon unit journal 无 warning。

安装期首次 `systemd-analyze verify` 在 Data symlink 创建前按预期拒绝不存在的 ExecStart；建立已授权
symlink 后四 unit 校验通过。首次人工 UDS curl 使用 SSH 用户被 `0660` 权限拒绝；改用 `eidolon`
身份后成功，证明 socket 没有为调试放宽权限。这两项均未通过临时 fallback 绕过。

## 剩余风险

- Data/SDK 的基础 dependency set 对窄 HTTP Authority 偏重，但属于 producer packaging 技术债；
  本轮不修改 sibling，也不在 Kernel 复制实现。
- 产品镜像仍需把同样的 migration、artifact 校验、备份、原子切换与 reboot gate 固化为可重复发布
  流程；本次是受控真机验证，不等于已经实现通用 installer/updater。
- Agent、Channel、Memory 不因本轮自动纳入 system manifest。
