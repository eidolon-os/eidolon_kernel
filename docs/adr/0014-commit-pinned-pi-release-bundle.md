# ADR-0014: Commit-pinned Pi upgrade bundle

- 状态：Accepted；Mac ARM64 dependency cache 与本地 offline prepare 已验证，真实 Pi 运行待验证
- 日期：2026-08-07
- Extended by：ADR-0015 的 8-source bundle、7 component 与 Channel 模型 hydration gate

## Context

ADR-0013 统一了 target-native activation，但 source staging 仍是人工步骤。当前八个仓库可以处在不同
branch；Data/Hub/SDK 的 HEAD 也可能继续推进。读取 working tree 或自动选择 HEAD 会把并行工作混进
产品 release，也不能为 descriptor revision 提供可复现证据。

新 release venv 必须按 Linux/aarch64 lock 构建，macOS venv 不能传输；但依赖 artifact 可以在 Mac
按目标 platform 预取并校验，避免 Pi 弱网络成为发布事务的一部分。与此同时 first-install
还涉及用户、Data V2 baseline、制造期 Host identity 和 secret provisioning，这些权限与生命周期不能
被普通软件升级脚本顺便接管。

## Decision

工作站 bundle builder 强制接收 Kernel/Data/Hub/Admin/Agent/Channel/Memory/SDK 八个完整 commit ID，并对每个 repo 执行
`git archive <exact commit>`。它不读取 working-tree 文件；archive 必须包含对应 lock 和 V2 固定系统
资产。Bundle manifest 固定 source 顺序、target、archive path 和 SHA-256，并包含同样有摘要的 standalone
target preparer。Builder 使用固定 uv 0.11.15 为 Python 3.13/aarch64 预取 frozen 依赖，把压缩、去重
cache 及 SHA-256、公开 index URL 和 build-tool pins 一并纳入 manifest。

Target preparer 只依赖 Python 标准库，在独立非阻塞 lock 下校验全部传输字节、拒绝绝对路径、`..`、
越界 symlink、device 等不安全 member，先提取 source/cache，再在 canonical release path 用固定
`uv sync --frozen --no-dev --no-editable --offline` 构建环境，最后调用新 Kernel 环境的 V2 sealer。受控错误只清理该调用新建的
release 目录，不修改 current links 或任何 persistent authority。

工作站 driver 使用 BatchMode SSH/SCP。默认止于 target prepare/seal 和 activation dry-run；真正切换
必须显式 `--resume --activate`，随后运行 doctor。输入限制为安全 release ID、host token、绝对本地输出
路径、绝对 target uv path 和八个 40-hex revisions；不使用 `eval`，不传输 secret。

## Consequences

- 已 provision Pi 的升级路径收敛为一个命令，且默认不会重启服务。
- Working-tree 并行修改不会进入 bundle；revision 与实际 archive 一一对应。
- Bundle SHA-256 只提供完整性，不是签名。SSH host key 是当前传输认证边界；未来 artifact signing 不能
  被 checksum 替代。
- 首次装机仍需要独立 provisioner。升级工具明确拒绝替它创建 Host identity、secret、Data baseline 或
  current links。
- 真实 Pi 的 cache transfer、prepare timing、dry-run、activation、故障回滚和 reboot 尚未
  在本提交执行，因此不能宣称一键路径已通过硬件验收。
