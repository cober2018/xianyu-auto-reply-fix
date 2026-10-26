# 开源项目补充能力与风控稳定性评估

## 2026-10-26 实现阶段结论

- 开发基线由用户明确指定为本地 `main@be22c9d`。服务器不可访问，不再用本机旧进程推断生产状态。
- 正常就绪序列来自固定版本 [goofish-cli wait_ready](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/core/ws.py) 与其[既有就绪 fixture](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/tests/test_ws_ready.py)：匹配注册回包成功后，还需 `/s/vulcan`；`ackDiff` 返回非成功不能单独证明注册失败。本地历史读取逻辑也识别 `/s/vulcan`。这些是协议实现证据，不能代替本账号现场帧。
- 只用实例身份不足以保护临时 HTTP 实例；只比较 Cookie 字符串又会把正常轮换变成永久停聊。最终采用进程内交接代次识别人工换会话，响应条件写回校验请求快照，认证前从现有数据库同步正常 Cookie 轮换。
- 暂停状态由数据库与内存共同门控；暂停写入和交接代次检查复用同一认证锁，旧结果不能在相同 Cookie 再次交接后反向暂停新会话。
- 私聊恢复和独立发货依旧分别记账。新增状态没有写入账号启停开关，也没有调整补发宽限、重试上限或内容快照。
- 热切换在取消当前实例前和等待后核对凭证快照；停用账号完成交接时也持久清标记，但不启动聊天。扫码保存失败、过时回调和失败回滚都有明确分支。
- 复用账号认证锁覆盖 Token、保活、浏览器恢复和手工导入。初始化只有一个读取者，早到推送保留到现有处理链；只有匹配 `/reg` 成功与业务就绪均到达，发送门控才开放。
- 既有回归覆盖原功能边界；新增状态迁移与持久化已在独立 SQLite 手工回读。未新增或修改测试代码，现场握手、新恢复路径并发联调及 72 小时观察仍缺运行证据。

## 历史：提案阶段补充

用户已认可研究方向。当前需求目录为 `openspec/changes/improve-chat-session-stability/`，规范草案见 [proposal.md](proposal.md)；后文保留研究时点证据，不能当作已实施回执。

- `XianyuAutoAsync.py:1068` 的账号暂停识别仅含 `account_risk_protected` 与 `manual_verification_required`；`:9639` 的 `captcha_recovery_disabled` 分支返回但不登记同类暂停，`:9741` 的硬拦截只保留有限退避，存在继续恢复的路径。
- 现有 `_apply_account_pause_state` 会影响账号启停。此次要求仅暂停受验证阻断的聊天认证，不把独立发货凭证链一起停用，因此不能直接把所有验证结果都映射到全账号停用。
- `reply_server.py:6373` 与 `:6424` 已有扫码后清理退避、等待旧任务退出、再次清理的交接，必须复用，并补充对当前实例/凭证的核对，避免旧失败结果覆盖恢复状态。
- OpenSpec CLI 1.2.0 已安装；已初始化本项目并生成提案 skill。仓库原有 `*.md` 忽略规则会隐藏规范正文，本轮仅为本需求目录放开 Markdown，保证可交接。

## 历史研究：频繁验证、私聊失效与扫码后等待

日期：2026-10-26。用户已确认：**程序不能私聊时，同一账号手机闲鱼 App 仍能手动发消息**。本轮完成公开源码、Issues、本地源码和可用运行证据核对；未执行账号实验、服务重启或代码实现。

### 判断

优先排查网页会话、IM Token 与程序恢复链路。手机能发送说明当时并非账号所有通道都被禁言，但不能单凭这一点区分网页端验证、凭证过期、浏览器环境差异与程序错误。仍缺少同一时刻电脑官方网页版能否正常收发的对照。

可借鉴的方向是复用有效会话、避免重复认证、将正常过期与验证阻断分开、按真实就绪状态恢复。此次检索没有找到可验证的“长期免风控”方案。其他卖家秒回不能证明其采用了哪一种实现，也不能证明其长期不掉线。

### 本地证据：运行状态与磁盘代码需要分开

| 已核实事实 | 证据 | 对决策的影响 |
| --- | --- | --- |
| 本机 8090 的 Python 进程 PID 94194 启动于 2026-10-08 23:37:48；cwd 是本项目，并持有当天日志文件 | `ps`、`lsof` 只读核对；当前 `main@be22c9d` 的相关提交发生在 10 月 26 日；`Start.py:471` 未启用 reload | 运行进程明显早于当前改动；不能把磁盘已修复当成服务已生效。尚未确定进程实际加载的提交，也未确认本机服务就是用户所说的生产实例 |
| 当天 15:37:00、15:48:52、15:59:55 仍重复进入验证与自动滑块路径，随后失败、退避、再重连 | `logs/xianyu_2026-10-26.log:15013`、`:15275`、`:15375`；返回含 `FAIL_SYS_USER_VALIDATE` / `RGV587`；日志函数行号与现源码不同 | 证明该时间段存在重复恢复循环；不能证明循环导致了最初的风控，也不能把所有同日日志统计成真实事故，文件还混有测试输出 |
| 当前磁盘配置已关闭自动滑块恢复，处理函数也先检查硬拦截与关闭开关 | `global_config.yml:90`、`XianyuAutoAsync.py:10104` 起 | 与上述运行行为不一致，先核对实际生效版本和配置，再评价已有修复；本轮没有重启 |
| 扫码稳定期默认 15 分钟，会挡住需要重新取 Token 的普通初始化/恢复；履约优先或存在待发货订单时有例外 | `global_config.yml:78`、`XianyuAutoAsync.py:698`、`:14915` | 部分等待来自本地策略，不是已证实的平台强制要求；不是每次扫码都必等 15 分钟 |
| 普通自动回复白天随机等待 2–10 秒，夜间等待 60–480 秒；自动发货另有白天 20–90 秒、夜间 180–900 秒延迟 | `XianyuAutoAsync.py:17920`、`:8094`；`global_config.yml:83` 起 | 当前策略本身妨碍持续秒回。没有本轮实测或官方依据证明这些具体延迟可以降低风控，代码注释不能当作平台规则 |
| 当前代码已有账号认证恢复锁、Token 刷新锁、成功后 60 秒复用、暂停门控与轻量保活；Token 本地复用间隔已是 20 小时 | `XianyuAutoAsync.py:9197`、`:14709`、`:14915`；`global_config.yml:127` | 不应重复建设或声称本地还在固定每小时取 Token；应检查各入口是否共同遵守现有状态与锁 |

扫码得到 Cookie、获得可用聊天 Token、WebSocket 注册与业务就绪、取得发送回执是不同步骤。前轮发现的业务就绪缺口与恢复后发信有关；批量推送处理缺口属于另一个消息可靠性问题，**不作为此次风控的已证实原因**。

### 扩展 GitHub 检索的可借鉴做法

以下均为源码/文档观察，不是用我们账号进行的稳定性实测。固定提交便于复核。

| 项目与版本 | 真实实现与可借鉴点 | 限制与取舍 |
| --- | --- | --- |
| [zhinianboke/xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply/tree/a58dea0d8ef43d4f467f09b5607af4821710700a) | Token 与 device_id 缓存、账号级刷新互斥、同账号验证处理中避免重复任务 | 缓存 TTL 是本地生成的 6–15 小时，部分分支允许尝试过期缓存；不是平台承诺的有效期。仍有滑块恢复链，不能整套搬来视作解决风控；本地单进程不因此新增 Redis |
| [fancyboi999/goofish-cli](https://github.com/fancyboi999/goofish-cli/tree/5b6b64ca72c54038874d57d61384765ee4e9b2be) | 按账号持久保存暂停和限流状态；浏览器续期后核验关键 Cookie；发信等待业务就绪与 ACK | 仍用同类网页 IM 协议；本机重置暂停不解除平台限制。默认 10 分钟暂停、普通写桶默认每分钟 1 次只是其本地策略，不能直接套到客服秒回 |
| [najiuwanan/xianyu-Plus](https://github.com/najiuwanan/xianyu-Plus/tree/20364a25573f1635d2ab32451d5be5c2e418ef9a) | 安全验证时停止该账号 Token 刷新、重试、心跳和重连；凭证更新后恢复；刷新互斥、错峰安排 | 这一整组暂停行为最值得对照。更新记录称按“真实到期时间”，但源码保存的是 `now + 20小时`，不是读取服务端有效期，不能照抄宣传或时间参数 |
| [G3niusYukki/realxianyu](https://github.com/G3niusYukki/realxianyu/tree/72e0ab746358512895ae9b5c85c1cfee18111a03) | CookieCloud/浏览器 Cookie 更新、限制恢复尝试、部分分支等待凭证变化；滑块成功后仍被阻断不会一律视作恢复 | 禁用滑块等部分分支仍会继续刷新和退避；“被挤爆”文字被代码解释为过载也不构成平台官方归因。不能认为接入后必然稳定 |
| [RickForYK/xianyu-auto-reply-cookiecloud](https://github.com/RickForYK/xianyu-auto-reply-cookiecloud/tree/2bb19bdc6cd2a6bcc3bbb96cc132c2646a9796d3) | 用浏览器扩展同步 Cookie，减少手工复制凭证；主循环停用了定时 Token 刷新任务 | 仓库已归档，最后推送在 2025-10-30；初始化仍需 Token，CookieCloud 是凭证同步，不是解除风控的通道 |
| [chinjiaqing/xianyu-im-client](https://github.com/chinjiaqing/xianyu-im-client/tree/913d9327ef6e5eb7d2605cf3a1548b45326b48cd) | Electron 中登录，后台取得 Cookie 和 Token 后接入聊天 | 桌面客户端外壳不意味着改用官方页面收发；`im.service.ts` 仍直连同一个 WS 域名，不构成避开当前故障机制的证据 |
| [huwangkeji/Xianyu-Smart-Customer-](https://github.com/huwangkeji/Xianyu-Smart-Customer-/tree/53842d24e0da903306f705235b8e93146155bca3) | 浏览器扩展监听官方网页消息，填输入框并发送，复用页面已有登录态 | 是值得验证的另一条收发路线。但发送函数最后直接返回成功，仅做粗略页面文本判断，没有可靠送达确认；不能直接替换我们的发货链路，页面改版也会影响选择器 |

关键源码：

- 上游 [Token 缓存](https://github.com/zhinianboke/xianyu-auto-reply/blob/a58dea0d8ef43d4f467f09b5607af4821710700a/websocket/app/services/xianyu/cookie_token_manager.py#L426-L465)、[账号刷新互斥](https://github.com/zhinianboke/xianyu-auto-reply/blob/a58dea0d8ef43d4f467f09b5607af4821710700a/websocket/app/services/xianyu/cookie_token_manager.py#L1075-L1103)。
- CLI [暂停状态](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/core/guard.py#L44-L80)、[浏览器续期结果检查](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/core/refresh.py#L105-L165)。
- Plus [验证后停止连接](https://github.com/najiuwanan/xianyu-Plus/blob/20364a25573f1635d2ab32451d5be5c2e418ef9a/src/main/java/com/xianyusmart/service/impl/WebSocketTokenServiceImpl.java#L898-L932)、[清理所有恢复任务](https://github.com/najiuwanan/xianyu-Plus/blob/20364a25573f1635d2ab32451d5be5c2e418ef9a/src/main/java/com/xianyusmart/service/impl/WebSocketServiceImpl.java#L920-L953)、[本地计算有效期](https://github.com/najiuwanan/xianyu-Plus/blob/20364a25573f1635d2ab32451d5be5c2e418ef9a/src/main/java/com/xianyusmart/service/impl/WebSocketTokenServiceImpl.java#L1073-L1082)。
- Realxianyu [恢复分支与等待](https://github.com/G3niusYukki/realxianyu/blob/72e0ab746358512895ae9b5c85c1cfee18111a03/src/modules/messages/ws_live.py#L1762-L1885)。
- Electron [实际 IM 传输](https://github.com/chinjiaqing/xianyu-im-client/blob/913d9327ef6e5eb7d2605cf3a1548b45326b48cd/src/main/service/im.service.ts#L49-L75)；浏览器扩展 [页面发送与成功判断](https://github.com/huwangkeji/Xianyu-Smart-Customer-/blob/53842d24e0da903306f705235b8e93146155bca3/content/platforms/goofish/index.js#L664-L689)。

补充证据：[shaxiu/XianyuAutoAgent #68](https://github.com/shaxiu/XianyuAutoAgent/issues/68) 曾报告运行约一天后 Token 刷新失败；该 Issue 已关闭，是历史使用者反馈，不能证明当前版本仍有同样问题，但也不能只用项目 README 当作长期稳定性证明。

官方也有[闲鱼小程序客服接入](https://open.goofish.com/doc/development/other/xspace.html)，需要业务与 CCO 配置，适用范围是特定服务商/小程序客服；该文档不证明普通卖家私聊可以直接接同一套接口。本轮没有为普通卖家确认到可直接替换当前 IM 的官方授权入口。

### 最小下一步与验收（建议，未进入实现）

| 顺序 | 最小动作 | 要回答的问题 |
| --- | --- | --- |
| 1 | 核对用户实际使用实例的进程、代码和有效配置，对齐已有修复后再评价稳定性；本轮只完成本机核对 | 当前是否仍在使用旧恢复逻辑？已关闭的自动滑块是否确实停止？ |
| 2 | 在同一次受限时对照手机 App、电脑官方网页版与程序；后续验证只使用明确授权的测试会话 | 若网页也能正常发，才有依据试浏览器常驻路线；若网页也被挑战，先处理网页会话恢复 |
| 3 | 沿用现有账号锁与凭证源，检查所有保活、Token、重连入口共同执行“需要验证时暂停，凭证有效更新后恢复”；复用仍有效的 Token | 能否减少重复认证和再次验证，而不是只增加等待时间？ |
| 4 | 以 Cookie 有效、Token 有效、注册与业务就绪为恢复依据，评估缩短普通扫码稳定期和不必要的回复延迟 | 能否做到扫码完成后及时恢复，且不因缩短等待形成重复请求？ |
| 5 | 仅当第 2 步支持该路线时，做单账号浏览器常驻试验；现有平台继续负责回复决策与履约记账 | 官方网页接管会话和收发是否比程序直连更少要求人工验证？ |

浏览器路线的具体增量是统一保留登录与 IM 所用会话。当前平台并非完全没有持久浏览器配置，但扫码验证分支 `utils/qr_login.py:287` 新建 context，`:340` 起关闭 context/browser；还不能视作登录与聊天全程共用一个常驻会话。无需因此直接替换整个框架。

建议后续用同一账号连续观察至少 72 小时，记录人工扫码/验证次数、Token 请求次数、聊天可用时长、扫码到聊天就绪时间、买家消息到回复回执的延迟，并确认测试对端实际收到。一次扫码成功或 WebSocket 已连接不能单独证明稳定性。此处为验收建议，未创建监控任务，也未生成或运行测试代码。

### 当前阶段与交接

- 本地代码位置 `/Users/mshengran/Project/xianyu-auto-reply-fix`，源码基线 `main@be22c9de97733aa53f1184a0009a3085b4c42409`；实际加载版本和生产实例对应关系待核实。
- 已完成风控专项研究；研究发现不等于已经修复。此次没有安装外部项目、重启服务、触发登录/验证、向买家发信或更改业务代码。
- 原研究成果已整体迁入本需求目录，根目录保留同名链接；本目录 Markdown 已纳入 Git 可见范围，目前未提交。
- OpenSpec 提案已生成并通过严格校验，下一步等待具体规范确认后 apply；本轮没有执行实现或 archive。
- 研究阶段使用 `using-superpowers`、`writing-plans`、`systematic-debugging`；后续提案阶段使用 `using-superpowers`、`writing-plans`、`openspec-propose`。

## 前轮：两个项目的通用能力比较（保留参考，不代表当前优先级）

日期：2026-10-26。状态：源码评估完成，未进入实现；不代表生产验收。

## 已核实的评估基线

- 本地 `be22c9d` 已包含发货凭证与私信独立记账、缺口巡检、原样补发、人工结案及逐单元完成判断；不能把旧 RFC 中的问题全部当作当前未修复问题。
- 本地 README 明确记载 `zhinianboke/xianyu-auto-reply` 是基础框架参考来源；当前远端已采用 FastAPI + React + MySQL + Redis 的分服务结构，与本地 SQLite 架构有明显差异。
- `goofish-cli` 0.6.3 提供 CLI/MCP、会话与消息读取、商品操作、账号限流及浏览器沙箱；其 README 仍把订单查询与发货列为未完成方向，需结合源码核实边界。
- 本次会话未提供 codebase-memory-mcp 图工具，代码发现使用限定文件的文本检索；记忆索引没有相关项目记录。

## 结论与版本

可以补充。优先吸收**批量推送逐条处理**与**发信前业务就绪确认**，保留当前平台的账号、订单、卡券和双通道履约职责。没有证据证明整套替换可以消除平台风控。

| 对象 | 核验版本 | 范围 |
| --- | --- | --- |
| 当前平台 | `main@be22c9de97733aa53f1184a0009a3085b4c42409` | `/Users/mshengran/Project/xianyu-auto-reply-fix`；未核验生产部署版本 |
| zhinianboke/xianyu-auto-reply | `a58dea0d8ef43d4f467f09b5607af4821710700a` | 2026-10-26 的公开源码；不能当作无冲突的直接上游合并 |
| fancyboi999/goofish-cli | `5b6b64ca72c54038874d57d61384765ee4e9b2be` | 0.6.3，2026-10-26；README 部分命令数与 roadmap 标签落后于代码 |

## 与痛点直接相关的发现

### 批量推送只处理第一条：已确认的代码缺口

本地链路为 `main` 接收完整帧 → `_enqueue_message` / `_handle_message_with_semaphore` → `handle_message`。队列模式和传统模式均原样传帧，没有先拆包；`XianyuAutoAsync.py:18023` 直接取 `syncPushPackage.data[0]`，`_unwrap_message_for_dedupe` 也只解第一条。

因此，当一帧有多个消息条目时，后续条目不会在这条实时链中逐条进入业务处理。整帧 ACK 已发出，不能证明所有条目处理过。可能影响买家消息、付款通知和会话资料；尚未用生产原始帧将此缺口归因到某一笔事故。

两个参考项目均逐条遍历：[xianyu-auto-reply message_handler.py:569–584](https://github.com/zhinianboke/xianyu-auto-reply/blob/a58dea0d8ef43d4f467f09b5607af4821710700a/websocket/app/services/xianyu/message_handler.py#L569-L584)、[goofish-cli core/ws.py:464–482](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/core/ws.py#L464-L482)。

建议迁移遍历思想，复用本地解码、付款路由、去重和履约逻辑。不能整段替换上游解码器，其 `chatType` 过滤与本地付款消息兼容逻辑存在差异。

### 连接建立与业务就绪混在一起：实现差异已确认，线上因果待验证

本地 `init`（`XianyuAutoAsync.py:14944` 起）发送 `/reg`、固定等待 1 秒、发送 `ackDiff` 后返回；主循环在 `19227` 行标记 CONNECTED 并可调度扫码后补发。`reply_server.py:4221` 的 `ws_ready` 依据连接状态及近期连接/心跳判断，未包含 `/s/vulcan` 业务信号。

CLI 的发送路径校验注册回包，等待 `/s/vulcan` 后才发送，再按 `mid` 等待发送 ACK。其源码将此顺序用于避免过早请求产生 code 400；这是参考实现的经验，不能推导我们所有 code 400 都由此导致。[业务就绪等待](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/core/ws.py#L164-L206)、[发送及 ACK 判定](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/commands/message/send.py#L77-L129)。

本地 `send_msg` 已有按 mid 等待 code 200 的机制（`14812–14914`），增量应集中在初始化就绪与恢复后的发送时机。服务端接受 ACK 与买家实际已读仍是不同事实。

### 双通道履约：保留已经完成的业务改造

本地已有 `delivery_channel_states`、逐单元缺口、原样快照补发、人工结案、宽限与退避、重连补拉订单，见 [双通道 RFC](../../../docs/rfc/RFC-20261026-dual-channel-fulfillment.md)。本轮核对的私信补发实现要求 sid；离线或退避期不新建连接补发；不重新分配卡密。

zhinianboke 当前 `ConfirmShippingService` 也支持 `tradeText`，不能声称其没有凭证直填。但单个接口支持不等于已满足我们的双通道账本和验收要求。[确认发货实现](https://github.com/zhinianboke/xianyu-auto-reply/blob/a58dea0d8ef43d4f467f09b5607af4821710700a/websocket/app/services/shipping/confirm_service.py#L37-L102)

CLI 当前命令目录没有订单模块，README 仍将订单查询/发货列为待办，不能替代履约主链。[命令目录](https://github.com/fancyboi999/goofish-cli/tree/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/commands)

## 后续可选增量

| 方向 | 我们已有能力 | 可借鉴内容及限制 |
| --- | --- | --- |
| 会话与历史 | `/api/chat/sessions`、`/api/chat/messages`、历史分页与本地回退 | HTTP+WS 合并、真实消息时间、消息 ID 去重、缺失/读取失败显式表达；短时发现不保证完整历史，不能拿 buyer_id 猜 cid |
| 登录续期 | 扫码后清理退避、Cookie 热切换、账号恢复锁与稳定期 | 续期结果核验关键凭证、按账号持久化冷却；本地运维 skill 已关闭密码重登，不能照搬上游密码兜底 |
| 商品发布 | 图片、类目、地址、批量发布、素材关联、发布后列表同步 | 保存已完成准备信息，区分 accepted / submission_unknown 并要求回查；已有发布和回查不能重复算作新增功能 |
| Agent 入口 | `skills/scripts/xianyu_api.py` + 本机 API | CLI/MCP 共用动作定义、结构化错误；如有 MCP 需求，优先包装现有 API，沿用权限及凭证源 |
| 浏览器环境 | 现有 Playwright/Chrome 与容器能力 | CLI 常驻浏览器沙箱可作为减少桌面打扰的候选；隔离环境不等于解决账号风控 |
| 运营功能 | 已有商品、卡券、通知等模块 | zhinianboke 的返佣、选品与发布规则属于新业务范围，单独评估，不并入本次可靠性改造 |

来源：[会话同步契约](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/docs/conversation-sync.md)、[发布状态与准备信息](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/src/goofish_cli/commands/item/publish.py#L65-L107)。

## 接入限制

1. zhinianboke 当前采用 React、MySQL、Redis，拆分 Web、WS、调度服务；为两个协议缺口迁移本地 SQLite 与现有服务体系不划算。
2. 本地 `requirements.txt:14` 要求 `websockets>=10.0,<13.0`，CLI 要求 `websockets>=13`，同一虚拟环境不能同时满足。[CLI 依赖](https://github.com/fancyboi999/goofish-cli/blob/5b6b64ca72c54038874d57d61384765ee4e9b2be/pyproject.toml#L22-L38)
3. 本地有稳定期和长退避；上游发信辅助函数默认失败后间隔 2 秒、最多追加 5 次重试；CLI 常驻 WS 异常重连退避上限为 30 秒。不能直接覆盖本地已有策略。
4. CLI 独立 Cookie 文件、刷新和 WS 不能成为本地第二个凭证源；优先移植具体方法或包装现有 API。

本轮只检查与当前痛点直接相关的路径，不构成对两个项目整体质量的审计结论。

## 最小验证顺序（尚未授权实现）

| 顺序 | 范围 | 验收证据 |
| --- | --- | --- |
| 1 | 单帧多条推送逐条处理，复用本地付款路由和逐条去重 | 普通消息与后续付款消息都进入处理；重复帧不重复履约；单条异常不吞后续有效条目 |
| 2 | 注册、业务就绪与发信时机分开 | 注册拒绝、迟到就绪、正常恢复可区分；未就绪不补发；恢复后取得匹配 ACK，账本只完成一次 |
| 3 | 仅在仍有证据缺口时补会话同步/回查 | CID、对端、消息 ID 与时间可核对；缺失保留，不假定全量历史已覆盖 |

前两项分别验证，便于判断收益。先用已有脱敏样本和验证入口；真实发信只使用明确授权的测试会话。不向真实买家试发，不在未明确要求时生成测试代码。

### 阶段与交接

- 已完成源码比较、调用链核对、优先顺序和限制整理；无业务代码改动。
- 未完成真实账号验证、线上事故归因、代码实现和生产验收。
- 下一步：确认“批量推送逐条处理 + 业务就绪确认”范围后运行 `/opsx:propose`；产出须明确确认后才能 apply，完成后 archive。
- 本地有 OpenSpec CLI，本轮会话未加载 `/opsx:*` 入口，也未建立 OpenSpec 变更。开发前应检查安装和会话加载，不能以本评估代替已确认规范。
- 实际使用 `using-superpowers`、`writing-plans`、`systematic-debugging`；排查止于源码证据与模式比较，未执行修复。

## 历史记录：安全审计（未在本轮复核，不能视为当前缺陷清单）

| 漏洞类型 | 漏洞位置 | 风险描述（大白话） | 修复建议 |
| :--- | :--- | :--- | :--- |
| **SQL注入** | `db_manager.py` -> `import_backup` | 导入备份时，直接把 JSON 里的 `columns`（列名）拼接到 SQL 语句中。如果有人上传恶意的备份文件，可以直接执行破坏性的 SQL 命令，甚至控制数据库。 | 严格校验 JSON 传入的 `columns`，或者直接从数据库的表结构（Schema）中获取列名，绝不信任用户上传的列名。 |
| **服务器端请求伪造 (SSRF)** | `notification_dispatcher.py` -> `_send_dingtalk_notification` 等 | 钉钉等消息通知的 `webhook_url` 是从用户配置直接读取的，没有做任何限制。恶意用户可以填入内网地址（如 `127.0.0.1`），让服务器去请求内网敏感服务。 | 在发送 HTTP 请求前，增加域名/IP 白名单或黑名单校验，明确禁止请求局域网（私有）IP 段。 |
| **硬编码密钥** | 历史记录：`reply_server.py` 的 `API_SECRET_KEY` | 旧审计指出存在备用硬编码密钥；具体值已从交接文档移除，本轮未复核该历史问题。 | 如后续处理，先核对现状，再移除硬编码并沿用环境配置。 |
| **弱密码哈希** | `db_manager.py` 的密码处理逻辑 | 系统在保存管理员和用户密码时，只用了单纯的 `SHA256` 进行加密，没有“加盐”（Salt）。这种方式很容易被黑客用“彩虹表”破解。 | 弃用纯 SHA256，改用 `bcrypt` 算法，或者在 SHA256 计算时加入随机的 Salt 字符串。 |
| **越权API/缺乏认证** | `reply_server.py` -> `/xianyu/reply` 接口 | 核心的消息回复接口没有任何身份验证保护。只要知道请求格式，任何人都能伪造数据请求这个接口，可能导致系统被滥用发送垃圾消息或引发报错崩溃。 | 在接口上增加鉴权依赖（如 FastAPI 的 `Depends(verify_token)`）或者增加严格的请求签名（Sign）校验机制。 |
| **代码混淆/潜在后门风险** | `secure_item_polish_ultra.py` | 使用了多层混淆（反转+Hex+Base64+Zlib）隐藏真实代码并通过 `exec()` 执行。虽然经解密分析，其真实代码仅为“商品擦亮”的正常 API 请求逻辑，并未发现外发 Cookie 等恶意后门行为，但这种黑盒设计严重违反安全规范，极易被替换为恶意 Payload。 | 建议完全废弃这种混淆方式，直接使用解密后的明文 Python 代码（即原 `ItemPolishModule` 类）进行替换。 |
