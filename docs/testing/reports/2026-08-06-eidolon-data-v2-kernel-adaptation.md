# Eidolon Kernel → Eidolon Data V2 适配验证报告

- 日期：2026-08-06
- Kernel 基线：`18ce039` 加工作树中既有的 eidolond directory routing 变更
- Data 只读基线：`2a33894` (`refactor: complete system data v2 boundary`)
- 范围：`eidolon_kernel`；未修改 `eidolon_data`、`eidolon_admin`、`eidolon_agent`

## 真实现状与决策

代码审计确认 Kernel 已经只有一个 Data 依赖：

```text
Attach/Reconciliation
  -> Kernel CompanionAuthority Port
  -> Kernel-owned strict consumed Schema/DTO/mapper
  -> authenticated GET /api/companion-authority/v1/companions/{companion_id}
  -> Data V2 Companion application/read authority
```

Kernel production package 不 import `eidolon_data`/SQLAlchemy/Alembic，不读取或写入
`eidolon-system.sqlite3`，不包含 Data V2 九张表，也不调用 Data 已删除的 Device、runtime、Event、
Owner/Companion CRUD 或旧 migration API。Producer 与 consumer schema 的结构语义完全一致；两边
不同的 `$id/title` 是刻意保留的 contract ownership，不是 drift。

Data V2 的 `audit_outbox` 只属于 Data 自己。Kernel mount transition 的状态、幂等结果和本地有序审计
仍在 Kernel 独占 SQLite 的一个短事务内提交；它们是低频 control-plane governance fact，不是
全局 audit index 或高频 telemetry。Hub/Data HTTP 校验发生在事务外。Kernel 不在事务内访问网络，
不向 Data outbox 双写，也不让 Admin 直开 Kernel SQLite。未来全局审计只能用独立 dispatcher 异步
批量发布已提交事实。

## 修改与门禁

- 更新 ADR-0006 的 Data legacy 过渡事实，新增 ADR-0009 固定 V2 依赖、审计和 SQLite 边界。
- Companion HTTP adapter 明确为 Data V2 consumer，并拒绝缺少有效 netloc 的伪 HTTP URL。
- consumed schema 标注已按 Data `2a33894` 核验。
- 新增架构测试：禁止 Data package/ORM、旧库/API/schema 与 Data V2 表进入 Kernel。
- 新增跨项目 contract 测试：比较 Data producer 与 Kernel consumer 的完整结构语义，并检查 V2
  clean baseline 只含九张 canonical authority 表。
- 新增真实进程 E2E：Alembic 从空库升级到 `0001_system_data_v2`，验证 exact tables、
  `integrity_check=ok`、外键无违规；启动真实 Data Authority、真实 Kernel uvicorn 和受控 Hub/directory
  dependency，执行成功 Attach、12 路相同 request 并发幂等、Owner mismatch、Companion missing、
  Data 停机 503、失败不写审计，以及旧 `/companions/{id}` 路由 404。
- 新增可复现 Kernel SQLite control-plane benchmark。

## 测试证据

以下先列 Data 适配完成时、包含既有 eidolond directory 工作树的实际分层结果：

| 层 | 命令 | 结果 |
|---|---|---:|
| Unit | `uv run pytest -q tests/unit` | 37 passed |
| Component | `uv run pytest -q tests/component` | 49 passed |
| Functional | `uv run pytest -q tests/functional` | 3 passed |
| Data V2 integration | `uv run pytest -q tests/integration` | 2 passed |
| Process/E2E | `uv run pytest -q tests/e2e` | 2 passed |
| Architecture + Contract + System | `uv run pytest -q tests/architecture tests/contract tests/system` | 48 passed |
| Full branch coverage | `uv run pytest -q --cov=eidolon_kernel --cov=eidolon_system --cov-branch --cov-report=term-missing` | 141 passed; 92.44% |
| Resource warning gate | `uv run pytest -q tests/e2e/test_data_v2_kernel_process_e2e.py -W error::ResourceWarning` | 1 passed |
| Lint | `uv run ruff check .` | passed |
| Import architecture | `uv run lint-imports` | 8 contracts kept, 0 broken |
| Compile | `uv run python -m compileall -q eidolon_kernel eidolon_system` | passed |
| Changed-file format | `uv run ruff format --check <changed Python files>` | 7 files formatted |

项目未配置 mypy/pyright/pyre，因此静态类型检查：**未执行（无项目工具）**。

为证明本提交没有暗中依赖用户未暂存的 eidolond/M2-B 文件，另用 `git checkout-index` 导出只含
Git index 的独立快照，设置真实 Data sibling 路径后执行同一完整覆盖率命令。结果为：

```text
120 passed in 6.13s
branch coverage: 92.19%
```

快照第一次运行的唯一失败是既有架构测试要求仓库根存在 `.git/`，而 `checkout-index` 天然不导出
Git 元数据；在临时快照补空 `.git` 目录后完整通过。Data V2 定向门禁在同一独立快照中为
6 passed（architecture 3 + schema integration 2 + real-process E2E 1），无 skip。

全仓 `uv run ruff format --check .`：**失败，39 个文件 would be reformatted**。这是当前 Ruff 0.16.1
对既有代码和用户未提交的 eidolond 工作树变更的格式差异；本适配涉及的全部 Python 文件已通过
同一 formatter。为保留用户无关改动，本提交不机械重排其余 39 个文件。

首次 E2E 调试出现两项已修复的测试基础设施失败：readiness client 意外继承本机 HTTP proxy；pytest
深层临时路径超过 macOS Unix socket 长度。最终版本显式 `trust_env=False`、使用短随机 UDS，并在
`ResourceWarning` 视为错误时通过。受限沙箱内直接绑定 socket 会被跳过/拒绝；上述最终 component、
system 和 E2E 结果均在批准的本机临时 socket 权限下完整执行，无 skip。

在本提交最终复核期间，另一路 M2-B systemd 部署工作并发新增了未跟踪的 `deploy/` 与
`tests/system/test_systemd_deployment.py`。当前混合工作树的最新全量结果因此为 **144 passed、
1 failed**；失败是该测试预期 `system-services.systemd.example.yaml` 已声明 Kernel unit，而 M2-B
尚未补齐 manifest。该文件、失败与本提交无关，未被暂存或修改；以上 Git-index 独立快照是本提交
自己的最终全量门禁证据。

## 性能诊断

命令：

```bash
uv run python scripts/benchmark_kernel_control_plane.py --iterations 500 --workers 4
```

当前 Darwin arm64 / Python 3.13.13 / SQLite 3.51.2 开发机结果：

- 500 次顺序完整 mutation（mount + idempotency outcome + audit）：p50 0.104 ms，p95 0.163 ms，
  max 0.601 ms；
- 4 thread 提交另外 500 次：p50 0.485 ms，p95 0.949 ms，max 2.000 ms，诊断吞吐
  7431.4 commits/s；
- WAL、`synchronous=FULL` (`2`)、foreign keys on、`integrity_check=ok`；
- 三张 Kernel 表均为 1000 行，证明每个 mutation 恰好一个 current/request/audit 写入。

该结果只证明本机短事务与显式 writer serialization，没有定义 Raspberry Pi 产品 SLA。进程级锁测试
证明第二个 Kernel writer fail closed；SQLite busy timeout 为 5 秒。目标设备仍需重复相同命令并按
真实 mount/reconciliation 规模设 SLA。

## 剩余风险与后续边界

1. Companion Authority 尚未进入已验证的 eidolond host manifest；Kernel 当前保留唯一 loopback
   base URL。Admin 部署先提供真实 supervisord/systemd target、readiness 和 service credential，
   再由 Kernel 改为 directory consumer；不得同时保留静态 fallback。
2. Kernel→Data 校验与 Kernel commit 不是分布式事务。Data lifecycle 在校验后变化时由 30 秒默认
   reconciliation 收敛；网络/认证/5xx 只 deferred，不能伪造成 Detach。
3. Kernel 本地审计尚未接入全局 audit dispatcher。Admin 不能通过直读 Kernel SQLite 填补；后续应
   消费 SDK `eidolon.audit.v1`，由 Kernel 独立 publisher 异步、批量、幂等投递到 rebuildable index。
4. Admin/Agent/Channel 中仍存在旧 Data ORM/schema 或 SQLite reader 的实际引用。它们必须各自迁移到
   Data V2 public application/read contract；Kernel 不提供 legacy shim，也不代替它们写 Data。
5. Admin 的 Device workflow 必须遵循：Hub 负责 admission，Kernel 负责 Mount/Attachment CAS，Data
   负责 Companion lifecycle/Guard policy。编排使用稳定 request ID 和可重试补偿，不跨三个 SQLite
   做同步双写或声明伪原子事务。
