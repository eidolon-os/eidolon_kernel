# ADR-0002: Companion 校验保持 Port，并消费事实拥有方的窄契约

- 状态：Accepted / Blocker resolved 2026-08-05
- 日期：2026-08-04

## Code-confirmed facts

兄弟项目代码中，`eidolon_data` 的 `companions` row 有 `companion_id`、`owner_id` 和默认值为 `active` 的字符串 `status`；status 没有 lifecycle enum/check constraint。Companion 可以被 service 硬删除。

`eidolon_data` 的 optional FastAPI 暴露未版本化 `GET /companions/{companion_id}`，没有显式认证 boundary。Admin 暴露本地主机编排 API，例如 owner-scoped list/create/delete，但没有独立的 versioned Companion authority read contract 或兼容承诺。

## Decision

Kernel 定义最窄 `CompanionAuthority.get_companion(companion_id)` Port，只需要返回 companion ID、owner ID、status。application 只接受 `status == active` 且 owner 匹配。

在事实拥有方发布稳定契约前，最初实现遵守以下 fail-closed 临时边界：

- production composition 使用 `UnavailableCompanionAuthority` 并返回 `503`；
- 不直连 `eidolon_data` SQLite，不导入兄弟 package；
- 不把 Admin orchestration API 当作 authority；
- 不自行定义跨项目 endpoint、status enum、token 或 lifecycle event；
- component/functional/E2E 只注入显式 test fake 验证 Kernel 自身纵向闭环。

## Resolution

`eidolon_data` 现已发布独立 Companion Authority App：只提供版本化精确 Identity GET，以 opaque Bearer service credential 认证，将底层所有非 `active` 状态收敛为稳定 `inactive`，且不返回 profile/runtime metadata。Kernel 使用独立 HTTP adapter、固定 consumed Schema、严格 binding 和 mapper；404 表示权威拒绝，认证、传输、5xx 与契约错误表示 authority unavailable。Kernel 仍不导入 Data package、不读共享 SQLite，也不依赖 Admin orchestration API。

临时 `UnavailableCompanionAuthority` 已删除，production composition 直接组装真实 consumer。运行时若 Data Authority 不可达，单次 Mount 明确失败，对已有 Mount 的周期对账只记 deferred，不把基础设施故障误判成 Companion inactive。
