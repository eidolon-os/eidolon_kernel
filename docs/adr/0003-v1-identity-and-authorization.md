# ADR-0003: Kernel V1 只建立一个 Owner 安全主体

- 状态：Accepted / Security limitation
- 日期：2026-08-04

## Context

Kernel 的 Device Mount 需要一个稳定主体同时回答两个问题：请求可访问哪个 namespace，以及审计归属于谁。V1 没有已被需求证明的服务主体委托、代操作链、sudo-like 提权或 session identity，因此同时建立 Owner 和 Actor 会制造两套可混淆的 principal。

`owner_id` 是 Kernel 自己定义的稳定、opaque 的 OS security/namespace principal，类似 UID。它不是账号资料、Persona、Companion 或业务对象。credential 只用于在某个信任边界证明 Owner，会过期、轮换或撤销，不能反过来成为 Owner identity。Kernel 不定义“一名 Owner 唯一一枚 token”，也不建立 token issuer。

当前 Hub owner-scoped management API 要求 Bearer credential，这是 Hub provider contract，不自动成为 Kernel identity root。Kernel 作为全局 authority consumer，应使用服务身份访问 Hub，而不是持有或转发某个 Owner 的终端用户 credential。

Device Mount 当前部署在单 Host，Kernel 只监听 loopback 或位于受信同机 ingress 后。远端产品入口的认证不属于 Kernel；Kernel 仍必须对每个读写和审计查询取得 Owner security context，并在自身边界执行 scope 检查。

## Decision

- Kernel V1 只有一个 principal：Owner；不建立 Actor entity、`actor_id` 或独立 actor credential。
- interface 依赖 `OwnerAuthorizer` Port，不依赖 JWT library、账号 profile 或具体 role。authorizer 返回当前 Owner context。
- V1 production composition 只提供 `TrustedLocalOwnerAuthorizer`：在 loopback 或已认证的同机 ingress 内，校验并接受显式 Owner hint。
- 该 adapter 不接受 Bearer credential，也不宣称完成互联网 authentication。
- 服务必须绑定 `127.0.0.1`，或由同机 ingress 保证远端不能直接设置/绕过 identity hints。关闭 trusted-local 配置时 composition 直接拒绝启动。
- 远端小程序、Web 或 Admin 用户只在产品 ingress 完成认证。ingress 向 Kernel 传递最小 principal；Kernel 只执行 Device Mount action/scope 授权，不解析终端用户 token。
- Kernel 调用 Hub 所带的 management token 只满足 Hub 当前 consumer contract。Kernel 不签发它、不把它当作调用者身份，也不将它继续传给其他服务。
- 所有 endpoint 的 Owner scope 只能来自 authorizer。Mount/Unmount body 和 Get/Resolve/List/Audit query 不接受目标 `owner_id`，防止调用者提交两套身份。
- Device Mount、幂等 outcome 和 audit 都记录同一个 `owner_id`。Device ID 可全局稳定，但跨 Owner Get/Resolve/List/Mutation/Audit 一律 fail closed；remount 不得转移 Owner。
- credential 只存在于 interface/adapter，不进入 Device Mount domain，不保存到 SQLite、幂等结果或 audit。
- Kernel 不成为 Owner profile/account authority，也不直连兄弟项目数据库或发明 Owner 资料 API。若未来出现独立 Owner lifecycle authority，必须由事实拥有方先发布窄 contract。

## Consequences

授权边界和审计责任由一个 Owner principal 清晰表达，避免 Owner/Actor 分叉。代价是 V1 不区分“Owner 亲自调用”和“受信服务代表 Owner 调用”；当前需求没有证明这种区分具有可执行的授权差异，所以不为它预建模型。

V1 不是可直接暴露到不可信网络的安全部署。对固定单机、Kernel local-only、远端认证终止于受信 ingress 的 headless 一体机，这一限制是部署 threat model，不是要求再造 JWT 的 blocker。

若未来 Kernel 直接接入不可信网络、跨 Host 调用，或同机开始运行不可信扩展进程，必须重新评估信任边界。届时可验证 principal、Unix domain socket peer credential、mTLS 或 capability 的选择应由部署和攻击面决定；不能把当前 hints 换个名字就宣称完成认证。只有出现真实的服务身份、委托或提权语义时，才另开 ADR 评估 Actor，而不是默认加回第二主体。
