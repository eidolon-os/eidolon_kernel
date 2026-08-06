# Data Directory 与 Companion Attachment 验证报告

- 日期：2026-08-06
- 范围：Kernel → eidolond directory → Data Companion Authority；Data/Hub 独立 capability health
- 部署状态：代码、本地真实进程 E2E 与 Pi release 准备已完成；Pi 持久化激活未执行

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

## Raspberry Pi 状态

新 release 已包含 Kernel、Data 与经审计的 SDK build；两个 venv 安装/import 成功。Data V2 baseline
已在独占 `/var/lib/eidolon/eidolon-system.sqlite3` 执行并通过 `validate_schema()`，Data secret 文件已
以 root-owned `0600` 准备。没有安装/替换 systemd unit、Polkit、运行配置或 current symlink，也没有
重启服务；这些持久化激活动作需要对准确范围的显式授权。

## 剩余风险

- Data/SDK 的基础 dependency set 对窄 HTTP Authority 偏重，但属于 producer packaging 技术债；
  本轮不修改 sibling，也不在 Kernel 复制实现。
- Pi 激活后必须验证四服务 cold start、独立 capability degradation、真实 Attach、整机 reboot、
  SQLite integrity 与 journal；未完成前不宣称 M2-C 真机闭环完成。
- Agent、Channel、Memory 不因本轮自动纳入 system manifest。
