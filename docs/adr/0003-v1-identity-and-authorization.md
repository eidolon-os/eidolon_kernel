# ADR-0003: V1 仅采用可信本机入口假设，不伪造 Kernel identity root

- 状态：Accepted / Security limitation
- 日期：2026-08-04

## Context

Hub 的 management JWT 是 Hub 自己的授权机制，不能自动成为 Kernel identity root。当前代码库没有统一、稳定、可由 Kernel 验证的 principal/token contract。直接复制 Hub HS256 secret、硬编码角色或信任任意公网 header 都会制造伪安全。

## Decision

- application 依赖 `ActorAuthorizer` Port，不依赖 HTTP header、JWT library 或具体 role。
- V1 production composition 只提供 `TrustedLocalActorAuthorizer`：在 loopback 或已认证的同机 ingress 内，信任显式 actor/owner hints，并把来源记录为 `trusted-local-ingress`。
- 该 adapter 不接受 Bearer credential，不宣称完成 authentication；actor owner 必须与请求 owner scope 相同。
- 服务必须绑定 `127.0.0.1`，或由同机 ingress 保证远端不能直接设置/绕过 identity hints。关闭 trusted-local 配置时 composition 直接拒绝启动。
- 每个 mutation 和 audit 都记录 actor。未来 identity root 通过替换 Port adapter 接入，不改变 Device Mount domain。

## Consequences

授权边界和审计责任显式存在，但 V1 不是可暴露到不可信网络的安全部署。统一 Kernel identity contract 是 production readiness blocker，不能通过给 header 换名字消除。
