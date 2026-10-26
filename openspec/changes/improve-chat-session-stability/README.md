# 聊天会话稳定性

当前阶段：**已融合本地 main，待运行验收**。需求 ID：`improve-chat-session-stability`。日期：2026-10-26。

## PRD / 需求背景

用户希望减少程序私聊被验证阻断、反复扫码与扫码后的长等待；同账号手机 App 在程序受限时仍可发消息。用户已确认本目录提案并要求开始开发；服务器不可访问，以本地 main 为开发依据。

## 代码位置与版本

- 当前代码与唯一成果目录所在工作区：`/Users/mshengran/Project/xianyu-auto-reply-fix`，分支 `main`。
- 实现提交：`6cfb35a8d519f331e93ace2491c9ded0a39af81f`；本地融合回执为其后续文档提交，当前版本以 `git rev-parse main` 为准。
- 本地基线：`main@be22c9de97733aa53f1184a0009a3085b4c42409`。
- 用户已授权融合本地 main，快进和合并后校验均已完成；未推送或部署。本目录在 main 中是实体目录，根目录兼容链接就地指向本目录，不再依赖开发工作树。
- 开发工作树为 Codex 本会话主工作区，归档工具拒绝删除，暂予保留；后续成果只维护 main 中本目录。

## 成果目录结构

| 文件 | 用途 |
| --- | --- |
| [proposal.md](proposal.md) | 目标、变更范围和影响 |
| [design.md](design.md) | 状态处理、恢复顺序、流程图与部署/回滚考虑 |
| [specs/chat-session-recovery/spec.md](specs/chat-session-recovery/spec.md) | 行为要求和验收场景 |
| [task_plan.md](task_plan.md) | 从原根目录迁入并持续维护的唯一实施计划 |
| [tasks.md](tasks.md) | 指向 task_plan.md 的链接，仅适配 OpenSpec 默认文件名 |
| [progress.md](progress.md) | 实际进展与验证结果 |
| [findings.md](findings.md) | 已有研究、固定版本源码证据及关键判断 |
| [review.md](review.md) | 提案自检、后续审计反馈与复核记录 |

根目录的 task_plan、progress、findings 为同一文件的兼容链接，不保存第二份内容。

## 已完成

- 扩展 GitHub 研究与本地源码/运行证据核对。
- 生成 proposal、design、spec 与实施计划，OpenSpec 严格校验通过；实施状态以 task_plan 为准。
- 初始化 OpenSpec Codex core profile；为本需求目录单独放开 Markdown 忽略规则，便于版本化交接。
- 完成持久聊天暂停、凭证交接、真实就绪门控与普通夜间回复调整；回归 80 项及隔离数据库迁移/持久化检查通过，详见 progress 与 review。

## 待完成 / TODO

- 本地实现、代码复核与 main 融合已完成；尚缺服务器联调及连续观察。
- 在可访问服务器环境核准实际版本、现场握手、对端接收、并发联调与至少 72 小时自然运行。
- 完成约定验收后 archive；浏览器常驻方案保留为有条件的后续试验。

## 关键设计决策

- 用一个聊天验证标记，复用账号锁和凭证源；聊天暂停不扩大成新的全账号停用。
- 有效扫码交接后只放行一次受控恢复，以注册确认和业务就绪决定是否可聊。
- 普通夜间回复默认 2–10 秒；保持发货延迟、双通道账本和补发宽限。
- 原有“批量推送逐条处理”不是此次风控提案范围；不迁移外部框架。

## 依赖与使用方式

- 提案工具：已安装 OpenSpec CLI 1.2.0；本项目通过 `openspec init --tools codex --profile core` 初始化。
- 会话尚未重新加载 `/opsx:*` slash 入口，本轮读取生成的 `openspec-propose` skill 并使用 CLI 完成对应提案流程。运行 `openspec status --change improve-chat-session-stability` 查看文件状态，运行 `openspec validate improve-chat-session-stability --strict --no-interactive` 校验规范。
- 业务依赖和启动方式沿用[项目 README](../../../README.md)，本提案没有安装新的业务依赖或启动服务。

## 下一步

部署、真实对端接收及 72 小时验收由后续可访问服务器的环境执行，完成后 archive。

开发阶段使用 skill：`using-superpowers`、`executing-plans`、`using-git-worktrees`、`openspec-apply-change`、`systematic-debugging`、`verification-before-completion`、`finishing-a-development-branch`。未生成或修改测试代码。

本地融合阶段使用 skill：`using-superpowers`、`writing-plans`、`finishing-a-development-branch`、`verification-before-completion`。
