# 设备会话 `effective_companion_id` 诊断：Kernel 不是断点

- 日期：2026-09-16
- 现场：opi5max（rk3588），release `rk3588-trace-why-1`（01:01 发布，服务 01:05 重启）
- 症状：每个设备语音会话的 trace 落成 `unknown-owner__unknown-companion__<session>.ndjson`；
  eidolon_channel 的 Tier B turn-event sink 对设备会话整条关闭
- 手段：全程只读（sqlite 只读查询、journald、loopback GET、离线复算）；未改动板子任何状态

## 结论

**Kernel 为这个挂载派生出了 Body endpoint，`present` 为 `true`，`effective_companion_id`
为 `c_129153685f855ff3b2062301fb3ceda0`，`conditions` 为 `["Realized"]`。三次设备会话，
Kernel 三次都答了 200，答案一致且正确。**

断点在 eidolon_channel 的 `observability/turn_events.py::_mounted_companion`：它按
`resolve_channel_context` 内部使用的两种形状去读 resolver 的返回值（包装器 `.runtime`、
裸连接 `.answering_companion_id`），而 `ChannelRuntimeServices.resolve_room` 交出来的是
第三种——`_resolve_context` 最后一行 `return resolved.runtime` 拆包后的裸
`ResolvedRuntimeIdentity`，Companion 就在它的 `companion_id` 字段上。两个 `getattr` 都落空，
函数掉进最后一个分支，而那个分支的句子把责任写给了 Kernel 挂载。

机主的推论「`endpoint is None`」不成立：见下文「为什么那条推论走不通」。

## 证据

### 1. Kernel 现在就答得对（loopback 只读 GET，无令牌）

```bash
curl -s -H "X-Eidolon-Owner: owner_129153685f855ff3b2062301fb3ceda0" \
  "http://127.0.0.1:8083/api/kernel/v1/body-endpoints/device-instance-d170051f11b4d1a4b7eb53a4a9bde5c11cbe81e24874cb905694b1a9079bffe4%3Abody"
```

`HTTP/1.1 200 OK`，关键字段：

```json
"present": true,
"assignment": {
  "companion_id": "c_129153685f855ff3b2062301fb3ceda0",
  "selection_provenance": "user_selected",
  "revision": 1, "generation": 1,
  "status": {
    "observed_generation": 1,
    "effective_companion_id": "c_129153685f855ff3b2062301fb3ceda0",
    "conditions": ["Realized"]
  }
}
```

### 2. 事故当时 Kernel 也答了 200（uvicorn access log）

```
$ sudo journalctl -u eidolon-kernel.service --since "2026-09-16 00:00" | grep -F "body-endpoints/"
Sep 16 00:26:38 ... 200 OK
Sep 16 00:32:34 ... 403 Forbidden   # 人工探测，带了 Authorization 头（V1 不接受，见 ADR-0003）
Sep 16 00:33:04 ... 403 Forbidden   # 同上
Sep 16 00:34:15 ... 200 OK
Sep 16 00:34:31 ... 200 OK
Sep 16 00:49:28 ... 200 OK          # 机主报告里那一次
Sep 16 01:12:44 ... 200 OK          # 重启后新会话，症状复现
Sep 16 01:17:23 ... 200 OK
Sep 16 01:18:21 ... 200 OK          # 本次诊断的 curl
```

三个 trace 文件（00:27:51、00:50:56、01:13:55）对应 00:26:38、00:49:28、01:12:44 三次
GET——**一次会话一次 Kernel 调用，全部 200**。每次会话只有一次调用，是因为
`ChannelRuntimeServices.resolve_room` 对结果做了记忆化：铸设备令牌的那一路和观测那一路
读的是同一个对象。

### 3. 从 2026-09-14 起这具 Body 的答案没有变过（审计链）

```sql
SELECT position, event_type, subject, subject_revision, occurred_at
  FROM kernel_audit_events WHERE device_id='device-instance-d170051f…';
```

```
5  eidolon.kernel.device-mounted-by-claim-event.v1  device-mount     1  2026-09-14T15:30:41.744783Z
6  eidolon.kernel.body-assignment-created.v1        body-assignment  1  2026-09-14T15:30:55.164130Z
```

此后没有任何事件。mount 仍是 `active=1, revision=1`，assignment 仍是
`companion_id=c_129…, user_selected, revision=1, generation=1`，
`kernel_schema_meta.schema_version = 8`（与代码 `SCHEMA_VERSION = 8` 一致，未触发拒绝路径）。
所以「01:18 的 200 内容 == 00:49 的 200 内容」不是猜测，是审计链证明的：期间没有写。

同 Owner 下 3 条 mount、3 条 assignment，全部 `active=1` / 全部 `user_selected` 指向同一个
Companion；没有离线记录可解析错。

### 4. 部署的代码就是本地 HEAD（排除「板子跑的是旧版」）

| 文件 | 板子 md5 | 本地 md5 |
|---|---|---|
| `eidolon_kernel/domain/body.py` | `4bc9941260b125ee31ce5de986fd7bb0` | 同 |
| `eidolon_kernel/application/body_assignments.py` | `f8cd8bfa02481e7aeacb7d4329414f50` | 同 |
| `eidolon_kernel/interfaces/http/router.py` | `dd5c7468228afe137ff84663111039df` | 同 |
| channel `observability/turn_events.py` | `0fce33e1e59709fd70b2628467dbb2d9` | = `eb79e63` 版本 |

注意最后一行：板子上的 channel 文件与 `eb79e63` 逐字节相同。

### 5. 离线复算：同一个函数，喂真实形状

把 `eb79e63`（板子在跑）和 `d622069`（修复）两版 `_mounted_companion` 用 AST 取出来单独执行，
分别喂三种对象：

```
== 板子在跑的 eb79e63 ==
   裸 ResolvedRuntimeIdentity（真实形状）
     -> companion='' reason='the Kernel mount has no Companion answering through this body'
   DeviceConnectionContext，无人应答
     -> companion='' reason='the Kernel mount has no Companion answering through this body'
   DeviceConnectionContext，有人应答
     -> companion='c_129153685f855ff3b2062301fb3ceda0' reason=''

== 修复后的 d622069 ==
   裸 ResolvedRuntimeIdentity（真实形状）
     -> companion='c_129153685f855ff3b2062301fb3ceda0' reason=''
   DeviceConnectionContext，无人应答
     -> companion='' reason='the resolver returned DeviceConnectionContext, which names no Companion'
   DeviceConnectionContext，有人应答
     -> companion='c_129153685f855ff3b2062301fb3ceda0' reason=''
```

第一行复现了板子 01:12:44 那句日志，逐字相同：

```
2026-09-16 01:12:44,775 WARNING agent.observability.turn_events
Channel turn events disabled: device event context incomplete: missing companion_id
(the Kernel mount has no Companion answering through this body)
```

同时看到旧版的第二个毛病：**真的没人应答**和**有人应答但读不出来**，两种情况被压成同一句话。

## 为什么「两个权威对同一件事答得不一样」这个说法要撤销

没有两个权威，也没有分叉。只有一次 Kernel 调用、一个答案、两个读者：

```
resolve_channel_context(kind="device")
  └─ mounts.resolve(...)                    → Kernel GET 200，answering_companion_id = c_129…
  └─ 非空 → runtime.resolve_companion(...)  → CompanionInteractionContext(context, mount_revision)
_resolve_context 最后一行: return resolved.runtime   ← 拆包
  └─ 裸 ResolvedRuntimeIdentity(companion_id="c_129…")   [pydantic, extra="forbid"]
       ├─ factory.py:135 → make_device_token_resolver → 设备令牌带 companion_id
       │    → eidolon_agent chat_servicer 读 identity.companion_id → 人格正常应答 ✅
       └─ factory.py:438 → runtime_context_resolver → _mounted_companion
            ├─ getattr(resolved, "runtime")                  → 不存在（已拆包）
            ├─ getattr(resolved, "answering_companion_id")   → 不存在（这个字段在 DeviceConnectionContext 上）
            └─ 掉进最后一句：「Kernel 挂载没有 Companion 应答」 ❌
```

语音通、trace 不通，是同一个对象被两个读者读——一个读对了，一个读错了形状。
`ResolvedRuntimeIdentity` 是 `extra="forbid"` 的 pydantic 模型，字段只有
`schema_version / owner_id / companion_id / memory_realm_id / genome_id / genome_hash /
realizer_version / device_id / interaction_mode`，既无 `runtime` 也无
`answering_companion_id`——两个 `getattr` 必然落空。

更强的说法：`_resolve_context` 第 182–184 行在拿到 `DeviceConnectionContext` 时是**抛异常**
而不是返回它。所以当 `context_resolver` 是 `resolve_room` 时，`_mounted_companion` 只可能
拿到两种结果：异常（走第一个分支，句子不同），或一个必然带非空 `companion_id` 的裸身份。
**那句「Kernel 挂载没有 Companion 应答」在这条装配下结构性地不可能为真——它每次出现都是错的。**

## 为什么机主那条推论走不通

推论是：`active=1 ⇒ present=True`，所以
`endpoint is not None and endpoint.present` 为假只剩 `endpoint is None`。

前半段对，后半段在 HTTP 面上不可达：

- `BodyAssignment.status(endpoint=...)`（`domain/body.py:271`）只有一个调用者，
  `contracts/mappers.py:98 assignment_to_wire`；而 `assignment_to_wire` 也只有一个调用者，
  `endpoint_to_wire`（`mappers.py:136`），它传的永远是刚 `resolve()` 出来的那个非 None endpoint。
  **`endpoint=None` 这半个分支在生产路径上打不到。**
- 想不出 endpoint，`BodyEndpoints.resolve()` 抛的是 `NotFound` → HTTP 404，
  channel 侧 `KernelBodyNotFound` 是异常，会走 `_mounted_companion` 的第一个分支，日志句子不同。
- 想让 `present=false`，channel 的 `_connection()` 有一条硬校验
  `document["present"] is not True → KernelBodyContractError`，同样是异常。

也就是说：**Kernel 侧任何形式的「没有派生出 endpoint / endpoint 不在场」，在 channel 侧都是响的
（异常），不是哑的。**观察到的是哑的，所以它不是 Kernel 侧那件事。

至于「`kernel_device_mounts` 没有 roles 列」——那是设计使然，不是缺口：`roles=("body",)` 由
`derived_endpoint()` 写死（`domain/body.py:141`），不读 manifest、不读 Hub。派生只依赖 mount 本身，
不存在「取不到东西就不产出 endpoint」这一步。模块顶部的注释已经把这条约定写清楚了。

## 当前状态

- 修复已由并行会话落在 eidolon_channel：`d622069 fix(channel): read the Companion off the
  shape the resolver actually returns`（2026-09-16 01:20:20），先读 `companion_id`，
  包装器与裸连接降级为兜底，最后一句改成报出实际拿到的类型。附带的测试用真的
  `ResolvedRuntimeIdentity` 替掉了原先那个「戴着 `.runtime` 的 SimpleNamespace」假替身。
  本次诊断是独立得到同一结论的，两边互为交叉验证。
- **板子上还没有这个修复。** `/opt/eidolon/current/eidolon_channel/.../turn_events.py`
  仍是 `eb79e63`（md5 `0fce33e1…`）。在下一次发布之前，设备会话仍会落成
  `unknown-owner__unknown-companion`。
- eidolon_kernel 侧本次未改任何代码，也不需要为这个症状改。

## 提给机主的方案

> 架构级方案见 [ADR 0018: 权威的答案必须带可执行的读法](../adr/0018-answers-with-enforceable-readings.md)。
> 下面四条是把现场恢复到可用所需的最小动作，不是架构结论。

按优先级四条：第 1 条是发布动作（不动代码），第 2、3 条在 eidolon_kernel，第 4 条跨仓。

1. **发布 channel 的 `d622069` 到 opi5max，然后用一次真机会话验收。**
   验收标准不是「日志没报错」，而是 trace 文件名变成
   `owner_129153685f855ff3b2062301fb3ceda0__c_129153685f855ff3b2062301fb3ceda0__<session>.ndjson`，
   并且 `agent.observability.turn_events` 不再打 `Channel turn events disabled`。
   这一条不需要动 Kernel。

2. **（Kernel）给 Body endpoint 的读加一行结构化日志。**
   这次为了回答「00:49 那一刻 Kernel 到底答了什么」，只能靠「30 分钟后重发一次 + 审计链证明
   期间没有写」这条推理补出来。审计链这次恰好干净，所以推理成立；但它不是通用手段——
   一旦期间有过 reconcile 或 remount，这个问题就答不了了。
   uvicorn 的 access line 只有状态码，没有响应体。建议在 `get_body_endpoint` 落一行
   `device_id / present / effective_companion_id / mount_revision / assignment_revision`，
   用 `eidolon_kernel` 自己的 logger（目前全仓只有 `adapters/reconciliation/periodic.py` 建了 logger）。
   这样下次同类问题是一条 grep，而不是一次跨仓推理。

3. **（Kernel，可选但建议）收紧 `BodyAssignment.status` 的签名。**
   `endpoint: BodyEndpoint | None` 广告了一个 HTTP 面产生不出来的状态，而机主的整条推论正是从
   这个分支起步的——代码的类型说它可能，事实上不可能，诊断因此走了一整圈弯路。
   两个方向任选：把签名收成 `endpoint: BodyEndpoint`、让 `CapabilityMissing` 只由 `present` 驱动；
   或者保留 `| None` 但在 docstring 里写明「唯一调用者恒传非 None，这半边是防御性的」。
   前者更彻底，改动面也小（只有一个调用者）。

4. **（跨仓，建议）为这个缝补一个契约测试。**
   两边的测试各自都是绿的：channel 那边用 `SimpleNamespace` 造了一个「戴着 `.runtime`」的假替身，
   kernel 那边的 schema 测试也过——而真实装配下这两半对不上。`d622069` 已经在 channel 侧补了
   用真类型的测试。真正缺的那条是端到端的：
   `ChannelRuntimeServices.resolve_room` 的返回值，必须是 `_mounted_companion` 认得的形状。
   这条断言该由 channel 持有（Kernel 不依赖 channel），但值得在 Kernel 这边的
   consumed-contract 文档里点一句：Kernel 只保证 wire document 的形状，
   **消费方在进程内怎么拆包，Kernel 的 schema 测试管不到**——这次出问题的恰好是那一段。

## 复现用的命令

```bash
# 1. Kernel 当前答案（只读，无令牌；V1 的 trusted-local 只认 X-Eidolon-Owner，带 Authorization 会 403）
curl -s -H "X-Eidolon-Owner: owner_129153685f855ff3b2062301fb3ceda0" \
  "http://127.0.0.1:8083/api/kernel/v1/body-endpoints/device-instance-d170051f11b4d1a4b7eb53a4a9bde5c11cbe81e24874cb905694b1a9079bffe4%3Abody"

# 2. 每次会话对应的 Kernel 调用与状态码
sudo journalctl -u eidolon-kernel.service --since "2026-09-16 00:00" | grep -F "body-endpoints/"

# 3. 证明期间没有写
sudo sqlite3 /var/lib/eidolon/eidolon-kernel.sqlite3 \
  "SELECT position,event_type,subject,subject_revision,occurred_at FROM kernel_audit_events
   WHERE device_id='device-instance-d170051f11b4d1a4b7eb53a4a9bde5c11cbe81e24874cb905694b1a9079bffe4';"

# 4. channel 侧那一半
sudo journalctl -u eidolon-channel.service --since "2026-09-16 01:05" | grep -F "turn events disabled"
md5sum /opt/eidolon/current/eidolon_channel/eidolon/livekit/agent/observability/turn_events.py
```

## 附：离线复算脚本（证据 5）

在 `eidolon_channel` 仓库里跑。用 AST 把两版 `_mounted_companion` 单独取出来执行，
避开整包导入，所以不需要 channel 的运行时依赖；喂进去的三种对象是这条装配下真实可能的全部形状。

```python
import ast, asyncio, sys
from dataclasses import dataclass
from typing import Any

class RRI:
    """Stands in for eidolon_sdk.biz.persona.ResolvedRuntimeIdentity (pydantic,
    extra=forbid): owner_id/companion_id/device_id and friends, nothing else."""
    def __init__(self, owner_id, companion_id, device_id):
        self.schema_version = "v1"
        self.owner_id = owner_id
        self.companion_id = companion_id
        self.memory_realm_id = "realm"
        self.genome_id = "g"; self.genome_hash = "h"; self.realizer_version = "r"
        self.device_id = device_id
        self.interaction_mode = None

@dataclass(frozen=True)
class DeviceConnectionContext:
    owner_id: str; device_id: str; mount_revision: int
    answering_companion_id: str | None = None

def load(path, name="_mounted_companion"):
    src = open(path).read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns = {"Any": Any}
            exec(compile(ast.fix_missing_locations(mod), path, "exec"), ns)
            return ns[name]
    raise SystemExit(f"{name} not found in {path}")

OWNER = "owner_129153685f855ff3b2062301fb3ceda0"
COMP  = "c_129153685f855ff3b2062301fb3ceda0"
DEV   = "device-instance-d170051f11b4d1a4b7eb53a4a9bde5c11cbe81e24874cb905694b1a9079bffe4"

cases = {
    # what _resolve_context actually returns for this Host: unwrapped identity
    "bare ResolvedRuntimeIdentity (real board shape)": RRI(OWNER, COMP, DEV),
    # what the Kernel says when nobody is assigned
    "DeviceConnectionContext, nobody answering": DeviceConnectionContext(OWNER, DEV, 1, None),
    "DeviceConnectionContext, someone answering": DeviceConnectionContext(OWNER, DEV, 1, COMP),
}

async def main():
    for label, path in (("deployed eb79e63", sys.argv[1]), ("fix d622069", sys.argv[2])):
        fn = load(path)
        print(f"== {label} ==")
        for name, obj in cases.items():
            async def resolver(room, _o=obj): return _o
            got = await fn(resolver, object())
            print(f"   {name}\n     -> companion={got[0]!r} reason={got[1]!r}")
asyncio.run(main())
```

```bash
git show eb79e63:eidolon/livekit/agent/observability/turn_events.py > /tmp/old.py
git show d622069:eidolon/livekit/agent/observability/turn_events.py > /tmp/new.py
python3 probe.py /tmp/old.py /tmp/new.py
```

## 验收结果（2026-09-17）

`d622069` 随 release `rk3588-claim-window-20260916` 发布到 opi5max，
kernel 00:03:09 / channel 00:03:44 起来（两者 `NRestarts=0`）。00:08:15 一次真机设备会话。

**四条全过，同一块板、同一个 device、同一个 Owner、同一个 Companion——是真正的前后对照，
不是在另一台机器上另证一遍。**

1. **部署确认**（按内容核，不只看 md5）：新兜底串 `which names no Companion` 1 处；
   旧兜底串 `the Kernel mount has no Companion answering` **0 处**；
   直读 `getattr(resolved, "companion_id")` 1 处。md5 `348b3b0e…`，与本地修复后一致。
2. **trace 归属正确**：
   `owner_129153685f855ff3b2062301fb3ceda0__c_129153685f855ff3b2062301fb3ceda0__esp32-67931301-18aa2b1f-00000001.ndjson`
   ——不再是 `unknown-owner__unknown-companion`。
   会话头记录里 `owner_id` / `companion_id` 也都是对的（即 `_base_payload()` 正常）。
3. **无 `turn events disabled` 警告**（发布后至今）。
4. **Tier B turn-event 流确实在走**——这是正面证据，不是"没报错"：
   `channel.turn.phase_changed` ×3、`channel.turn.milestone` ×9、
   `channel.turn.completed` ×1、`channel.session.ended` ×1。
   原症状说 phase_changed / milestone / terminal 三族**全部早退**，现在三族都在。

这次会话读的 Body 正是诊断里那一个：
`device-instance-d170051f11b4d1a4b7eb53a4a9bde5c11cbe81e24874cb905694b1a9079bffe4`，
Kernel 答 200。

第 3 条单独不算数（没开会话也不会有警告），是第 2、4 条把它坐实的。

ADR 0018 仍是 Proposed，未执行——它本来就不负责修这次，见该文"各条对本次 bug 的实际作用"。
