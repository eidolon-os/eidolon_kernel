# ADR-0016: Raspberry Pi FHS Host path contract

- 状态：Accepted；隔离实现与测试完成，现有 `/srv` Host 替换清理和真实 Raspberry Pi 验收待明确授权
- 日期：2026-08-09
- Extends：ADR-0014、ADR-0015

## Context

早期 prepared release 把不可变代码和 current symlink 放在 `/srv/eidolon`。完整产品运行图又需要明确区分
代码、配置、持久状态、临时运行态、日志、缓存和 Bootstrap 状态；继续从 `HOME` 或单一产品目录派生这些
路径，会让 systemd sandbox、备份、reset 和 ownership 边界含混。

已经在线的 Raspberry Pi 仍使用 `/srv/eidolon`。修改代码中的 canonical path 不会自动替换在线 Host，
也不能把一次跨根目录切换当成没有额外恢复语义的普通 `update`。

## Decision

新 prepared target 的代码与 release selector 固定为：

```text
/opt/eidolon/releases/<release_id>/...
/opt/eidolon/current/<component> -> ../releases/<release_id>/<component>
```

其余生命周期按 FHS 和 authority 拆分：

- root-owned 配置和 secret：`/etc/eidolon`；
- Eidolon authority state：`/var/lib/eidolon`；
- Bootstrap authority state：`/var/lib/eidolon-bootstrap`；
- 临时运行态：`/run/eidolon` 与 `/run/eidolon-bootstrap`；
- 日志和缓存：`/var/log/eidolon`、`/var/cache/eidolon`。

Ops provisioner 原子写入 root-owned `/etc/eidolon/host.env`，systemd units 显式读取它。固定 ExecStart 仍指向
`/opt/eidolon/current`，避免任意环境变量把 root service 导向未审阅代码。Kernel release descriptor、
sealer、target preparer、systemd assets 和 Raspberry Pi driver 使用同一 canonical namespace。

现有 `/srv` 代码树不迁移。Ops 的受管扩容事务只把旧 4-component links 当作 ownership 和失败恢复证据：
先在 `/opt` 准备 exact-commit full release，再由正常 Kernel activation 快照旧 system assets、分阶段切换
7 个 links/22 个 assets 并执行 readiness。失败恢复旧 units/links，并清理尚未激活的 `/opt` bridge links；
成功后必须再次通过 `/opt` doctor 与 App-ready，才删除 exact `/srv/eidolon` tree。事务不得复制或移动旧
release 文件，也不得把 `/var/lib/eidolon`、`/var/lib/eidolon-bootstrap` 或 `/etc/eidolon` 纳入删除范围。
真实 Host 上的删除仍需明确授权。

## Consequences

- 新装机代码、配置、数据和 runtime lifetime 不再混在同一目录，也不依赖登录用户 `HOME`。
- `/etc/eidolon/host.env` 是密封的 Host 路径契约，不是 secret store，也不改变各组件数据库 authority。
- 历史 ADR 和测试报告继续保留 `/srv`，因为它们记录的是真实旧部署证据，不应重写历史。
- 纯净新 Host 可以直接采用 `/opt` 契约；现有 `/srv` Host 只能经 Ops 的受管 `expand` 替换事务切换，
  不能直接把此提交当作普通 update。
- `/srv/eidolon` 是成功门禁后的遗留代码清理目标，不再是要长期维护或迁移的数据路径。
