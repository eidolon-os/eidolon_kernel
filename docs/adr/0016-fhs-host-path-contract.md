# ADR-0016: Raspberry Pi FHS Host path contract

- 状态：Accepted；隔离实现与测试完成，现有 `/srv` Host 迁移和真实 Raspberry Pi 验收待明确授权
- 日期：2026-08-09
- Extends：ADR-0014、ADR-0015

## Context

早期 prepared release 把不可变代码和 current symlink 放在 `/srv/eidolon`。完整产品运行图又需要明确区分
代码、配置、持久状态、临时运行态、日志、缓存和 Bootstrap 状态；继续从 `HOME` 或单一产品目录派生这些
路径，会让 systemd sandbox、备份、reset 和 ownership 边界含混。

已经在线的 Raspberry Pi 仍使用 `/srv/eidolon`。修改代码中的 canonical path 不会自动迁移在线 Host，
也不能把一次跨根目录切换伪装成普通 `expand` 或 `update`。

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

本 ADR 不授权在线迁移。已经存在 `/srv/eidolon` current links 的 Host 必须继续使用旧发布矩阵，直到 Ops
提供独立的迁移事务：预检旧拓扑、在 `/opt` 准备 exact-commit release、快照旧 links/assets、分阶段切换、
健康门禁失败恢复旧 units/links，并在重启后验证。新 Kernel commit 在该事务完成前不得成为现有 Host 的
默认 pin。

## Consequences

- 新装机代码、配置、数据和 runtime lifetime 不再混在同一目录，也不依赖登录用户 `HOME`。
- `/etc/eidolon/host.env` 是密封的 Host 路径契约，不是 secret store，也不改变各组件数据库 authority。
- 历史 ADR 和测试报告继续保留 `/srv`，因为它们记录的是真实旧部署证据，不应重写历史。
- 纯净新 Host 可以直接采用 `/opt` 契约；现有 `/srv` Host 在迁移实现和硬件故障恢复验收前不能直接使用
  此提交做普通 update/expand。
