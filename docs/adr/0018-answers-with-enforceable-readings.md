# ADR 0018: Body Mesh 的读契约放回它已经在的地方

Status: Accepted，已落地（第二稿。第一稿提了五条决定，其中三条经复核撤回——撤回理由见文末
"撤回的第一稿"，那一节是本 ADR 的一部分，不是附录）

决定 1 的一处子条款在执行中被证伪并据此修正：`ReplaceAssignmentResult.status`
**没有**一并收紧。理由与其余六项与假设不符之处，见文末"执行时发现的与 ADR 假设不符的地方"
——那一节同样是本 ADR 的一部分。

落地提交：`eidolon_sdk` 声明读契约、`eidolon_kernel` 改为生产 canonical 类型、
`eidolon_channel` 与 `eidolon_admin` 改为以消费方身份 import。四者是 lockstep：
SDK 先行，否则另三个仓不可编译。

触发事件：[2026-09-16 opi5max 设备会话归属诊断](../diagnosis/2026-09-16-device-session-effective-companion.md)

## Problem

设备会话的 trace 落成 `unknown-owner__unknown-companion`。Kernel 三次会话三次答 200 且正确；
断点在消费方进程内，消费方把自己的读取失败写成了 Kernel 的原话。

第一稿据此提了五条决定。复核之后，其中四条要么是补丁、要么是重复造轮子、要么其核心论证不成立。
真正的结构性事实只有一条，而且它比第一稿小得多：

> **Body Mesh 的 canonical wire 契约已经在 `eidolon_sdk.device_foundation.v1.body_mesh` 里了。
> Kernel 没有用它——写路径在本仓私自重新声明了一遍，读路径则根本没有声明——
> 所以消费方没有一个 canonical 的地方去读"读路径长什么样"，
> 只能镜像常量、grep 生产方的源码。**

三条可复核的证据：

1. **SDK 已经拥有 Body Mesh 的 wire model，而且不止 enum。**
   `eidolon_sdk/device_foundation/v1/body_mesh.py` 里有
   `AssignmentMode` / `SelectionProvenance` / `AssignmentCondition`（词表）、
   `BodyTargetRef`、**`ReplaceAssignment`、`ReplaceAssignmentResult`**（wire model）。
   也就是说：**写路径的 wire 契约是共享的，读路径的不是。**
   同一个概念，边界在中间断了一刀。
2. **Kernel 没有在用它。** `contracts/bindings.py` 只 import 了 `DeviceRef`；
   `ReplaceAssignment` 被在本仓重新声明为 `ReplaceAssignmentRequestWire`；
   全仓搜不到 `ReplaceAssignmentResult` 或 `BodyTargetRef` 的任何引用。
   对这个生产方而言，SDK 里那套 body-mesh wire model 是**已发布的死代码**。
3. **消费方的代价正是这条断刀。** `eidolon_channel` 已经依赖 eidolon_sdk
   （`kernel_bodies.py` 从中 import `DeviceRef`），它声明的边界是"不依赖 Kernel"——
   而 `DERIVED_ENDPOINT_ID` 与读法都在 Kernel。于是它镜像常量，并用
   `assert 'DERIVED_ENDPOINT_ID = "body"' in body.py` 对生产方源码做子串匹配；
   两条跨仓测试都在缺兄弟 checkout 时 `pytest.skip`，以通过的形式消失。

第一稿列的其余症状——`status` 是 `{"type": "object"}`、否定答案没有名字、
跨仓校验靠 grep——全部是这一条断刀的下游表现，不是四个独立问题。

## 一个必须一并记下的冗余

`status` 是 `(present, companion_id, generation)` 的纯函数，而这三者**都已经在同一份文档里**：

```python
"effective_companion_id": self.companion_id if (endpoint is not None and endpoint.present) else None
"observed_generation": self.generation
```

也就是说 `status` 不携带任何新信息。它的存在让"谁在这具 Body 上应答"在同一份文档里有了
两个可读的地方，消费方因此必须被用散文告知该读哪一个——`kernel_bodies._answering()`
的 docstring 正是在做这件事。

这不是本仓自造的：spec/status 是 canonical 资源模型的形状，`observed_generation` 也是
刻意为"将来出现第二个 actor"留的。但这套模式的收益（有一个会滞后的第二 actor）在本产品
**不存在**，而 `domain/body.py` 自己的注释已经承认了：
"there is no second actor to lag behind"。
所以：**收益还没到，成本已经在付，而这次的诊断代价就是从这笔成本里出的。**
本 ADR 不提议删掉 `status`（那是偏离 canonical 形状，代价另算），
只要求这笔成本被知情地支付，并在决定 1 落地时把它记在 SDK 的定义旁边。

## Decision

**一条。** 把 Body Mesh 的读契约声明在它的写契约已经在的地方：

1. `BodyEndpoint`、`BodyAssignment`、**受约束的** `BodyAssignmentStatus`
   （`additionalProperties: false`，`required: [observed_generation, effective_companion_id,
   conditions]`，`conditions` 为 canonical enum 数组），以及 `DERIVED_ENDPOINT_ID`，
   声明在 `eidolon_sdk/device_foundation/v1/body_mesh.py`，与已在那里的
   `ReplaceAssignment` / `ReplaceAssignmentResult` / 词表并列。
   `ReplaceAssignmentResult.status` 同批次一并收紧——它今天也是 `dict[str, object]`，
   **只改 Kernel 私有的那份会让 Kernel 比 canonical 更严，是加深断刀而不是弥合它。**
2. Kernel 以生产方身份 import 并据以校验；`contracts/bindings.py` 里的重复声明与
   `contracts/schemas/body-mesh/` 要么退化为对 SDK 定义的包装，要么删除。
3. 消费方以消费方身份 import。`_DERIVED_ENDPOINT_ID` 的镜像、
   `assert ... in body.py` 的源码 grep、以及缺席即 skip 的两条测试，随之删除。
   这不违反 eidolon_channel"不依赖 Kernel"的边界——它本来就依赖 SDK。

**不新建 conformance kit，不造 golden 文档，不加新的测试装置。** 机制已经存在、已经发布、
两侧已经在用，缺的只是把它用完。

## 代价（必须写明，否则这条决定看起来是免费的）

- **发布顺序被倒置。** 今天改 Body Mesh 读形状是 Kernel 本地动作；此后必须 SDK 先行。
  这是真实成本。抵消它的理由是：写路径和词表**已经在付这个代价**，读路径不付才是异常。
- **SDK 有变成杂物间的压力。** 判据要写死：进 `device_foundation/v1` 的，
  只能是**跨权威的 wire 事实**；Kernel 的内部聚合（`DeviceMount`、`BodyAssignment` 的
  domain 版本、SQLite 行形状）绝不进去。
- **一次消费方可见的契约收紧。** 收紧 `status` 不改变任何现有字段的值，
  对当前 wire 输出是 no-op；此后任何 `status` 变更会在消费方测试里显形——这正是目的。

## 不属于本 ADR 的两件事

- **`AssignmentCondition` 要不要加"在场且无人应答"。** 第一稿把它当作一条独立决定，
  复核后降级：`selection_provenance` 已经命名了四种安静里的三种
  （`user_cleared` / `companion_deleted` / `policy_reconciled`），
  真正无名的只剩 `assignment: null`（从未被指派）这一种。
  它是决定 1 落地时在 SDK 里顺手决定的一个局部问题，不值得为它单独走三仓 lockstep。
- **为会话铸造身份的读要不要留证据。** 值得做，但它是运维改进，不是架构决定。
  已留在[诊断报告](../diagnosis/2026-09-16-device-session-effective-companion.md)的行动项里。

## 撤回的第一稿，以及撤回的理由

第一稿有五条决定。保留这一节，是因为**撤回的理由比决定本身更有价值**——
其中一条的核心论证是错的，而它读起来很像对的。

| 第一稿 | 判决 | 理由 |
| --- | --- | --- |
| 1. 在 Kernel 私有 schema 里约束 `status` | **撤回** | 补丁，而且方向错。SDK 的 `ReplaceAssignmentResult.status` 同样是 `dict`；只改本仓会让 Kernel 比 canonical 更严，**加深**断刀。并入决定 1，改在 SDK 做 |
| 2. 给 `AssignmentCondition` 加 `Unassigned` | **降级** | 论证是错的。我写的是"消费方就写不出那句错话了"——**没有任何机制强制消费方引用 condition**，那句错话是 channel 源码里的字符串字面量，加一个 enum 值不影响它。且与 `selection_provenance` 大部分重复，真实缺口只剩 `assignment: null` |
| 3. 发布 conformance kit（golden 文档 + 期望抽取） | **撤回** | 重复造轮子。共享 SDK 就是跨仓契约机制，两侧都已在用；再造一套 golden 文件 + 测试装置是并行设施 |
| 4. 为身份铸造读留结构化证据 | **移出** | 一行 log 挂了"架构"的名。而且它没有回答自己提出的架构问题（这类读该进 `kernel_audit_events` 还是进程日志），只是绕过了它。是运维项 |
| 5. channel 一条会话身份、按类型读、禁止 `getattr` 默认值降级成领域结论 | **保留** | 成立、最小、且是五条里**唯一能拦住这次 bug** 的一条。不在本仓，且已由 `d622069` 落地 |

第一稿的比例本身就是症状：五条决定、三个仓、一条 lockstep 发布顺序，
而对应的缺陷其真实修复是一个已经落地的 30 行 diff，
并且我自己在表里承认五条里有四条拦不住它。
**当一份架构方案里的大多数条目都拦不住触发它的那个缺陷时，
先要怀疑的不是缺陷太小，而是方案在借题发挥。**


## 执行时发现的与 ADR 假设不符的地方

七条。第一条推翻了决定 1 的一处子条款，其余六条不改变决定，但改变这份 ADR
对现状的描述——而那份描述正是决定的依据。

### 1. `ReplaceAssignmentResult.status` 不能收紧（**决定被修正**）

决定 1 要求把它和读路径的 `status` 收成同一个受约束的类型，理由是"只改 Kernel
私有那份会让 Kernel 比 canonical 更严"。**这两个 `status` 不是同一份文档**，
本仓之外的证据是 canonical 契约自己发布的一致性向量
`DF-BODY-REPLACE-ASSIGNMENT-RESULT-VALID`
（`eidolon_sdk/contracts/device_foundation/v1/examples/valid/body-mesh.json`）：

```json
"status": { "observed_generation": 0, "conditions": ["PendingRealization"] }
```

三处与 ADR 开出的约束冲突：**没有 `effective_companion_id` 这个键**
（`grep -rn effective_companion_id contracts/` 在 SDK 里零命中——它根本不是
canonical 词汇，是 `domain/body.py` 自己造的字）；`PendingRealization`
不在 `AssignmentCondition` 里；`observed_generation: 0` 落后于 `generation: 1`。

第一点和第三点不是两个巧合：canonical 的 `status` 描述的是**实现滞后于 spec**
的资源，读路径描述的是一次事务里同时提交两者的资源。本 ADR 的"一个必须一并记下的冗余"
一节已经引了 `domain/body.py` 的 "there is no second actor to lag behind"，
但把它读成了一笔要知情支付的成本，没读出它正是"这两个 status 形状不同"的证据。

照字面执行会让 canonical 绑定拒绝本仓自己发布的向量，而且**不会有任何测试报警**：
一致性 runner 只拿 JSON Schema 校验 fixture，从不拿 Python 绑定校验
（`conformance/run.py:191`）。

落地取的是不动冻结契约的一路：`BodyAssignmentStatus` 是新增的严格读路径类型，
`ReplaceAssignmentResult.status` 仍是开放对象，旁边写明原因，并有一条测试把这处分叉
钉住——fixture 哪天与词表对齐，那条测试会失败，这个决定就会被重新做一次而不是被忘记。

顺带：撤回第一稿决定 1 的那句理由（"会让 Kernel 比 canonical 更严"）本身也不准确。
轴不是严格程度，是这两份文档根本不同。

### 2. 写路径并没有被"重新声明"

事实 2 说 `ReplaceAssignment` 在本仓被重新声明为 `ReplaceAssignmentRequestWire`。
两者只共享 `expected_assignment_revision` 一个字段。canonical 命令有
`body_endpoint_id` / `mode` / `policy_refs`，Kernel 的请求都没有；Kernel 的请求有
`operation` / `request_id` / `origin` / `change_reason`，canonical 命令都没有。
而 `origin` 正是 SDK 模块自己那段 docstring 解释过的东西——provenance 由写入权威
从"谁在改"推导，调用方不得断言。**这不是副本，是本 Host 承载那条命令的传输形状。**
它和 `replace-assignment-request.schema.json` 因此保留。

真正断掉的只有读路径一刀，不是两刀。

### 3. canonical 契约里从来没有过读路径

不止 SDK 的 Python 模块没有：`body-mesh/schemas.schema.json` 的 `$defs` 只有
`EnsureMount` / `EnsureMountResult` / `ReconcileEndpoints` / `ReplaceAssignment` /
`ReplaceAssignmentResult`——**全是命令和命令结果，没有任何读资源**。所以标题里的
"放回它已经在的地方"是不准确的：读契约从未在 canonical 里待过。

这不影响决定的可执行性——`SelectionProvenance` 和 `AssignmentCondition`
本来就只以 Python 词表存在、没有对应 `$def`，新增读类型是照着同一个先例走。
但"机制已经存在、已经发布、缺的只是把它用完"这句定性只对**机制**成立，
对**内容**不成立：这次确实发布了一块新的公共面，只是没有新造机制。

### 4. Kernel 的读文档是投影，不是 canonical 端点

canonical 的端点声明只有 `endpoint_id` / `roles` / `assignment_policy` /
`risk_class` / `concurrency` 五个字段。Kernel 的 `kernel.body-endpoint` 另外带了
`device_id` / `owner_id` / `device_ref` / `mount_revision` / `source` / `present`
和整个 `assignment`——这些是 mount 的事实。它是**跨权威的 wire 事实**，所以按 ADR
写死的判据可以进 `device_foundation/v1`；但它是 Kernel 形状的，不是 canonical
资源本身，所以进去的类型上带着 `operation: "kernel.*"` 判别符。这一点值得记下来，
因为下一次有人拿这条先例往里放东西时，判据仍然是"跨权威 wire 事实"，
不是"Kernel 发出去的东西"。

### 5. Python 消费方是三个，不是两个（**已补做**）

`eidolon_admin` 也把读路径完整重新声明了一遍——
`server/eidolon_admin_server/app/control_plane/contracts.py` 里的
`KernelBodyEndpoint` / `KernelBodyAssignment`，同样是 `status: dict[str, Any]`，
同样靠一个 `.get("effective_companion_id")` 的 property 把散文变成读法。
它也已经依赖 eidolon_sdk（`pyproject.toml` 里是 editable path 依赖）。

最初按"ADR 和委托都只写了三仓三步"没有动它，后经指示补做，作为第 4 步落地。
两个本地声明换成 import；两个 property 不是权威陈述的事实，所以没有跟着进 canonical
类型——`effective_companion_id` 变成对 `assignment.status` 的直接带类型读取，
`assignment_revision` 变成模型旁边一个具名函数。分页信封留在本地。

补做时另外发现一处：`server/tests/body_mesh_support.py` 的 docstring 声称
"消费方契约测试会拿这个形状和生产方的 schema 比对"——**body-mesh 从来没有这样一条测试**，
`test_control_plane_contract.py` 只比对 device-mount。也就是说这一仓对读路径的跨仓校验
一直是零，而注释让它读起来像有。已改正，并补了两条测试钉住旧的无类型 `status` 看不见的漂移。

### 5b. 非 Python 消费方仍然只能镜像

`eidolon_admin/web/src/api/controlPlane.ts` 里还有第四份 `KernelBodyEndpoint` /
`KernelBodyAssignment`——TypeScript 接口，结构化、不校验、只声明页面读哪几个字段，
**完全没有 `status`**。它没有被改，而且用现在这套机制也改不了：接第 3 条，
读路径没有 canonical JSON Schema，而 Dart / C++ 绑定生成器只覆盖有 `$def` 的类型
（`generation/generate.py` 的三语言符号闸门只读 admission / delivery-port / events）。

所以本次消除的是**Python 消费方**的重复声明，不是全部。任何非 Python 消费方要摆脱手抄，
前提是读路径先有一份 canonical schema——那正是第 1 条里被推迟的那个取舍。
这一份今天不是缺陷：那一列叫 "Body assignment"，显示的是 assignment 记录本身
（revision / generation / spec 的 companion / provenance），而 mount 的 active 与否
就在紧邻的一列。但它读的是 spec 而不是 status，与本 ADR 起因的那次读法只隔一个意图。

### 6. `consumer-matrix.json` 现在是过期的

`requirements/consumer-matrix.json` 里 `eidolon_channel` 的 `contracts` 是
`["common", "delivery-port"]`，不含 `body-mesh`。第 3 步之后不再成立。
没有一并改，因为那份清单在冻结契约目录里、其摘要进了 `generated/catalog.json`，
改它属于"动冻结契约"那一类——与第 1 条同一个已经做出的取舍。

### 7. 假设成立的部分（一条）

"收紧对当前 wire 输出是 no-op"——**成立，且已被证明**。
`tests/functional/test_device_mount_http.py` 新增的一条测试把整份响应文档按字面写死，
第一次运行即通过，没有改动过任何一个字段。所以这次收紧没有拒绝本 Host 已经在发的东西，
也没有发现"有人一直在发不合规的东西"。
