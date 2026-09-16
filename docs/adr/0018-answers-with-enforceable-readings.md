# ADR 0018: Body Mesh 的读契约放回它已经在的地方

Status: Proposed（第二稿。第一稿提了五条决定，其中三条经复核撤回——撤回理由见文末
"撤回的第一稿"，那一节是本 ADR 的一部分，不是附录）

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
