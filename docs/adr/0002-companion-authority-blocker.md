# ADR-0002: Companion 校验保持 Port，生产 adapter 暂时 fail closed

- 状态：Accepted / Blocker recorded
- 日期：2026-08-04

## Code-confirmed facts

兄弟项目代码中，`eidolon_data` 的 `companions` row 有 `companion_id`、`owner_id` 和默认值为 `active` 的字符串 `status`；status 没有 lifecycle enum/check constraint。Companion 可以被 service 硬删除。

`eidolon_data` 的 optional FastAPI 暴露未版本化 `GET /companions/{companion_id}`，没有显式认证 boundary。Admin 暴露本地主机编排 API，例如 owner-scoped list/create/delete，但没有独立的 versioned Companion authority read contract 或兼容承诺。

## Decision

Kernel 定义最窄 `CompanionAuthority.get_companion(companion_id)` Port，只需要返回 companion ID、owner ID、status。application 只接受 `status == active` 且 owner 匹配。

在事实拥有方发布稳定 contract 前：

- production composition 使用 `UnavailableCompanionAuthority` 并返回 `503`；
- 不直连 `eidolon_data` SQLite，不导入兄弟 package；
- 不把 Admin orchestration API 当作 authority；
- 不自行定义跨项目 endpoint、status enum、token 或 lifecycle event；
- component/functional/E2E 只注入显式 test fake 验证 Kernel 自身纵向闭环。

## Exit criteria

事实拥有方需要发布：版本化精确读取接口、严格 wire schema、active/inactive/deleted 语义、owner 关系、认证/授权方式、错误语义和兼容策略。契约稳定后再实现 HTTP consumer，并用 provider/consumer contract test 锁定。
