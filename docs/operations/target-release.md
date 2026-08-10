# Raspberry Pi target release runbook

本文描述 Kernel 拥有的 target-native 发布事务。Mac 上的唯一日常入口是独立
`eidolon_ops/eidolon-pi`；这里的 `eidolon-release` 是被它复用的低层 root 执行器，不负责 SSH、
基础系统 provision 或 secret 制造。

## 固定发布范围

- 8 个精确 Git 输入：Kernel、Data、Hub、Admin、Agent、Channel、Memory、SDK。
- 7 个运行组件与 active link；SDK 只作为构建支持源。
- 22 个 allowlist 系统资产、11 个 mode/ownership 受检私密前置文件。
- 14 个产品 unit：Bootstrap、eidolond、Data、Data Workspace、Hub、Kernel、Local API、Admin、
  NATS、LiveKit、Memory Supervisor、Memory Discovery、Agent、Channel。
- 12 个独立 readiness：UDS、HTTP、HTTPS、TCP 与 systemd active 探针。

这表示“完整产品后端”，不是把每个开发进程都搬上 Pi。`client-web`、Audit worker、Vision 和手机
App 二进制不在本 release descriptor 内；手机 App 通过 Bootstrap/Local API 管理这台 Host。

## Commit-pinned bundle

所有 revision 都必须是完整 40-hex commit ID。工具对每个仓库执行 `git archive <commit>`，不会读取
working tree。Channel 的 8 个模型允许在 commit 中保存标准 Git LFS pointer；builder 把 pointer bytes
直接交给 `git lfs smudge`，再按 pointer 的 SHA-256/size 复核导出对象并写入 archive。它不读取 Channel
working tree；LFS object 缺失、下载失败、digest 不符或导出后仍是 pointer 都会 fail closed。

```bash
.venv/bin/eidolon-release bundle <release_id> /absolute/path/to/bundle \
  --kernel-repo /path/to/eidolon_kernel --kernel-revision <40hex> \
  --data-repo /path/to/eidolon_data --data-revision <40hex> \
  --hub-repo /path/to/eidolon_hub --hub-revision <40hex> \
  --admin-repo /path/to/eidolon_admin --admin-revision <40hex> \
  --agent-repo /path/to/eidolon_agent --agent-revision <40hex> \
  --channel-repo /path/to/eidolon_channel --channel-revision <40hex> \
  --memory-repo /path/to/eidolon_memory --memory-revision <40hex> \
  --sdk-repo /path/to/eidolon_sdk --sdk-revision <40hex> \
  --uv /path/to/pinned/uv-0.11.15
```

Bundle 固定 source 顺序、目标 `linux/aarch64`、archive 路径和 SHA-256。Mac 用同一批 frozen lock
为 Python 3.13 的 `aarch64-manylinux_2_40` ABI 预取依赖，并把压缩 uv cache、公开 index URL、固定 uv/build-tool 版本及整包摘要
写入 manifest。摘要只检测传输/磁盘损坏，不是发布签名或来源认证。

## Target-native prepare 与 seal

```bash
sudo python3 /path/to/bundle/prepare_target.py /path/to/bundle \
  --uv /usr/local/bin/uv
```

Preparer 只依赖 Python 标准库。它在非阻塞 preparation lock 下重新校验所有字节，拒绝绝对路径、
`..`、越界 symlink/device 等 unsafe tar member，在
`/opt/eidolon/releases/<release_id>/` 提取 8 棵 source，并用
`uv sync --frozen --no-dev --no-editable --offline --python-platform aarch64-manylinux_2_40`
从已校验 cache 为 7 个运行组件建立 Pi 原生环境。预取与安装使用同一 platform selector，避免目标机
glibc 比通用 Linux 预取基线更新时选择另一个未缓存 wheel URL。
Data 明确安装 `api` extra。Pi prepare 阶段不访问 Python index；任一步失败删除本次新建的 release 与
临时 cache，current link、secret 和数据库不变。

## Activation 状态机

```text
exclusive lock -> sealed preflight -> snapshot assets/existing links
  -> quiesce ingress + eidolond + children
  -> install 22 allowlist assets -> switch 7 links -> daemon-reload
  -> start Bootstrap -> eidolond -> Local API -> Admin
  -> eidolond reconciles NATS/LiveKit/Data/Hub/Kernel/Memory/Agent/Channel
  -> 12 readiness -> receipt
       failure -> restore exact snapshot -> rolled_back
       restore failure -> rollback_failed and stop automation
```

从已管理的 4-component core release 扩展时，preflight 只允许 Agent、Channel、Memory 三个新增 link
不存在。Snapshot 因而保存 4 个旧 link；失败恢复会删除三个本次新增 link，并依据 asset snapshot 只恢复和
启动原先存在的 unit。缺失 Kernel/Data/Hub/Admin link、混合 release target 或非 symlink 仍在停服务前拒绝。
Ops 必须先用显式 `expand` 阶段安装新服务输入；普通 `install` 不收养已有 namespace。

Dry-run 执行完整 sealed preflight，但不创建 snapshot、不停服务、不切换：

```bash
sudo /opt/eidolon/releases/<id>/eidolon_kernel/.venv/bin/eidolon-release deploy \
  /opt/eidolon/releases/<id>/release.json --dry-run
```

实际激活去掉 `--dry-run`。`eidolond` 仍是 10 个 system service 的唯一 desired-state authority；
deployer 只在发布事务中执行有序 quiesce/start，不直接写 eidolond 或兄弟服务数据库。

## Schema、配置与数据边界

发布事务强制 `database_migrations=[]`。它不创建、迁移、备份、复制或恢复任何 authority SQLite。
Bootstrap 当前代码、候选代码与数据库 `user_version` 必须相同；不兼容 schema 在停服务前拒绝。
Data V2 首次 baseline 属于 `eidolon-pi install`，不允许导入旧迁移或旧 `eidolon.sqlite3`。

11 个 required secrets 只检查固定 path/mode；Agent、Channel、Memory 的 3 个 settings YAML 由首次安装
按 `root:eidolon 0640` 注入。代码发布、schema gate、配置/secret、生命周期和数据备份是五条独立边界。

## Doctor 与 rollback

```bash
sudo /opt/eidolon/current/eidolon_kernel/.venv/bin/eidolon-release doctor \
  /opt/eidolon/releases/<id>/release.json

sudo /opt/eidolon/releases/<id>/eidolon_kernel/.venv/bin/eidolon-release rollback \
  /opt/eidolon/releases/<id>/release.json \
  /var/lib/eidolon/deployments/<id>-<transaction_id>
```

Doctor 重新验证 descriptor、source/lock/venv/entrypoint fingerprint、22 个资产、11 个前置文件、
7 个 active link、unit 与 12 个 readiness。Rollback 只恢复 snapshot 中的 allowlist 资产和原有 link；
拓扑扩展前不存在的新增 link 会被删除。Rollback
不恢复 secret、Host identity 或数据库；因此它是代码/系统资产回滚，不是数据时间旅行。

当前实现已通过本机隔离事务与故障注入；真实 Pi 的 native build、systemd/BlueZ/NetworkManager、重启、
断电与手机 commissioning 必须在明确授权的隔离硬件上验收。
