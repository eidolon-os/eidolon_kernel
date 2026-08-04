# ADR-0003: V1 在信任边界认证一次，不为 Kernel 新造 JWT

- 状态：Accepted / Security limitation
- 日期：2026-08-04

## Context

JWT 是 credential 的一种 wire format，不是 identity architecture。当前代码确认存在不同用途的 token：Hub management JWT 保护 Hub owner-scoped 管理接口，Agent runtime JWT 承载 Channel 到 Agent 的会话身份，LiveKit JWT 由外部平台协议要求。它们属于不同 trust domain，不能因为格式相同就合并，也不应继续为 Kernel 增加第四套 JWT。

Device Mount 当前部署在单 Host，Kernel 只监听 loopback 或位于受信同机 ingress 后。Kernel mutation 仍必须留下 actor 和 owner scope，以便授权和审计；但 actor attribution 不要求每个本机 hop 都重新执行密码学认证。

## Decision

- application 依赖 `ActorAuthorizer` Port，不依赖 HTTP header、JWT library 或具体 role。
- V1 production composition 只提供 `TrustedLocalActorAuthorizer`：在 loopback 或已认证的同机 ingress 内，信任显式 actor/owner hints，并把来源记录为 `trusted-local-ingress`。
- 该 adapter 不接受 Bearer credential，不宣称完成 authentication；actor owner 必须与请求 owner scope 相同。
- 服务必须绑定 `127.0.0.1`，或由同机 ingress 保证远端不能直接设置/绕过 identity hints。关闭 trusted-local 配置时 composition 直接拒绝启动。
- 远端小程序、Web 或 Admin 用户只在产品 ingress 完成认证。ingress 向 Kernel 传递最小 principal；Kernel 只执行 Device Mount action/scope 授权，不解析终端用户 token。
- Kernel 调用 Hub 所带的 management token 只满足 Hub 当前 consumer contract。Kernel 不签发它、不把它当作调用者身份，也不将它继续传给其他服务。
- Kernel 不解析 LiveKit JWT 或 Agent runtime JWT，不读取其 shared secret，不建立 universal token。
- 每个 mutation 和 audit 都记录 actor。未来 identity root 通过替换 Port adapter 接入，不改变 Device Mount domain。

## Consequences

授权边界和审计责任显式存在，但 V1 不是可直接暴露到不可信网络的安全部署。对固定单机、Kernel local-only、远端认证终止于受信 ingress 的 headless 一体机，这一限制是部署 threat model，不是要求再造 JWT 的 blocker。

若未来 Kernel 直接接入不可信网络、跨 Host 调用，或同机开始运行不可信扩展进程，必须重新评估信任边界。届时可验证 principal、Unix domain socket peer credential、mTLS 或 capability 的选择应由部署和攻击面决定；不能把当前 hints 换个名字就宣称完成认证。
