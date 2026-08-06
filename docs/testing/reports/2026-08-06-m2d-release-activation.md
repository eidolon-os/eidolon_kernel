# M2-D Prepared Target Release 测试报告

- 日期：2026-08-06
- 范围：release wire contract、target sealing、Linux preflight、原子激活、自动/显式回滚
- 当前状态：本地实现与全仓门禁完成；Raspberry Pi 验证待 clean commit 后补录

## 已验证边界

- `eidolon_deploy` 与 Kernel/eidolond runtime package 整包 independence；runtime 不 import 部署实现；
- descriptor 只接受 Kernel/Data service component 和一个真实 Data SDK support source；
- target、release/current path、system asset、secret、unit、readiness 与空 migration 均是固定 allowlist；
- source/lock/installed distribution/asset checksum、Python version、entrypoint、secret mode 与 systemd unit
  在停服务前一次性检查；
- 排他 host lock 阻止并发 activation；dry-run 不执行持久 deployment mutation；
- snapshot、资产安装、symlink 替换、receipt 均使用本地原子替换；
- quiesce 后任一步失败都会进入 restore；restore 失败单独上报，不能伪造成功；
- 后续独立进程可从 root-only snapshot 显式恢复；snapshot path、component target 和 asset set 均重新校验；
- secret 和 SQLite 从不进入 release snapshot，V1 migration 非空直接拒绝。

## 本地结果

部署专项：

```text
63 passed
eidolon_deploy coverage: 92%（包含 branch；门槛 90%）
Ruff: passed
Import Linter: 9 contracts kept, 0 broken
```

全仓在允许本机 loopback/Unix socket 的执行上下文中完成真实进程 E2E：

```text
217 passed in 16.70s
combined branch coverage: 92.25%（门槛 90%）
Ruff: passed
Import Linter: 9 contracts kept, 0 broken
uv lock --check --no-cache: passed
```

首次受限运行出现的两个 loopback bind 权限错误已通过上述非受限全量运行复核，不是代码失败，也没有
用 skip 或降低阈值规避。

## Raspberry Pi 验证清单

目标地址已更新为 `192.168.100.15`，且新地址的 ED25519/RSA/ECDSA host key 已与旧设备逐项匹配。
clean commit 后依次执行：native preparation/seal、dry-run、成功 activation、故障注入自动 rollback、
显式 rollback、最终 activation、整机 reboot、eidolond/Data/Hub/Kernel readiness、Device Mount/
Companion Attachment authority 恢复与四个 SQLite integrity check。不得修改或切换 Admin/Hub release。
