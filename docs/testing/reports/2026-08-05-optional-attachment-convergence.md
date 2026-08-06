# Optional Attachment 与消费者边界收敛测试报告

- 日期：2026-08-05
- 范围：Kernel、Channel、Data、Agent；Memory 不在本轮范围内
- 目标：解除 Device 与 Companion 的必然绑定，消除第二套 Device authority 和已失效的
  Hub/NATS command adapter，同时保持现有 audio pipeline 不变

## 已验证语义

- Device 可在没有 Companion 时进入同 Owner Kernel namespace；Mount 不访问 Companion
  Authority。
- Companion attachment 是显式、可选、同 Owner、CAS/幂等的状态迁移；Unmount 会原子结束
  attachment，历史关系只留在 audit。
- Device、Companion 可分别形成 Channel connection/interaction context；无 Device 的虚拟
  Companion 可进入完整 interaction，无 Companion target 的 Device 不进入 audio/Agent 路径。
- Channel 的 Kernel Resolve consumer 严格 owner scope，并以 Kernel normative Schema 做
  跨仓 shape drift 门禁。
- Companion lifecycle/master promotion 不再隐式创建 Web Device；Guard claim/disable 不再
  修改 Device owner、approval、status、mount 或 attachment 字段。
- Data-backed Hub registry adapter、Agent 对已删除 Hub command API 与 NATS runtime-device
  blackboard 的 adapter 已删除；没有自行发明替代跨项目契约。

## 测试与覆盖率

```text
Kernel full + branch coverage: 87 passed; 92.54% (gate 90%)
Data full:                     56 passed
Data diagnostic branch cov:   56 passed; 73% (repository has no gate)
Channel affected:             31 passed
Channel broad regression:     1449 passed, 7 skipped, 39 deselected
Agent full:                   518 passed, 1 skipped, 2 failed
Agent branch coverage gate:   518 passed, 1 skipped, 2 deselected; 81.15% (gate 77%)
```

Channel broad regression 在非沙箱环境运行，因为 gRPC/WebSocket mock 需要绑定 loopback；
沙箱运行的端口权限错误不记作代码失败。该回归排除了当前仓库两份已知失败文件：
`tts/sensetime/test_tts_persistent.py` 和 `test_tts_pool_cancel.py`。其余 STT/TTS、VAD、EOT、
interrupt、Agent 与配置测试均包含在 1449 个通过用例中。pytest-cov 注入曾触发
NumPy/LiveKit native module 重复加载，故本轮没有伪造 Channel 覆盖率数字。

Agent 全量的两个失败位于未修改代码：低信号对话的 memory fanout 期望与当前 policy 不一致，
以及 260-token budget 下 recent history 期望与当前 harness 开销不一致。覆盖率命令只 deselect
这两个已知失败以验证 77% gate，不表示它们已修复。

Data 覆盖率是全 package 的诊断数字，包含未由本轮调用的 legacy CRUD API、CLI 和 migration；
运行时另报告两个既有 SQLAlchemy unclosed SQLite `ResourceWarning`。73% 不是质量门禁，也不应
被解释为 27% 功能必然错误。

## 静态与架构门禁

```text
Kernel Ruff:          passed
Kernel Import Linter: 4 kept, 0 broken
Data Ruff:            passed
Channel changed Ruff: passed
Channel YAML parse:   passed
Agent changed Ruff:   passed
Agent Import Linter:  3 kept, 1 broken
```

Agent 的 broken contract 是未修改的
`infra.persistence.eidolon_data_runtime -> domain.history` import；全仓 Ruff 另有 19 个未修改
文件中的既有问题。本轮修改文件 Ruff 全部通过，不把存量债务误报为已解决。

## 剩余 blocker / 演进门槛

1. 当前本机 composition 尚未启动 Kernel，Channel Provider 也尚未稳定写入受信
   `owner_id`；因此 Kernel Mount consumer 必须保持默认关闭。
2. Data 的 `DeviceRow`/repository 与显式 `ensure_web_body` 仍被 Admin/旧消费者使用，只能作为
   legacy compatibility surface；移除前必须先迁移真实消费者，不能让新代码写入准入/Mount
   事实。
3. Agent `body_control` 只有在 Channel 发布 Owner-scoped Provider/capability listing、幂等
   command submission 和 terminal receipt 契约后才能重新启用。
4. attachment 目前只是一个可选默认关联，不是 ACL、永久 pairing、当前 session route 或通用
   多对多资源图；没有第二个真实关系需求前不扩展 Kernel。

本轮没有引入 NATS/Redis/gRPC/Binder/Blackboard/ResourceGraph，也没有修改
`eidolon_memory`。
