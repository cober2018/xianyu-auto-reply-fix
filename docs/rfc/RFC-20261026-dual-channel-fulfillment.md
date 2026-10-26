# RFC-20261026：闲鱼履约双通道改造（发货凭证直填 + 私信通知独立记账/巡检/补发）

状态：已实现（本文为实施记录） ｜ 日期：2026-10-26 ｜ 前置：RFC-20261025-trade-text-direct-delivery

## 1. 背景与问题

2026-10-26 订单 5127563270248090346（买家「核动力纸飞机」）首次走通发货凭证直填，
但同单的私信补充告知因「扫码登录稳定期」长连接未就绪而失败。当时的实现里：

- 直填成功即把订单标记履约完成，私信失败只打一条 warning 日志（P1 静默放弃）；
- 履约巡检只扫「平台已发货但无完成记录」，已完成的订单不再进入视野（P2 巡检失明）；
- 无任何告警提示运营方「平台发货成功但私信未送达」（P3 运营无感），
  最终由运营方在手机 App 手工补发。

## 2. 设计决策

### 2.1 双通道各自记账（新表，不动 finalize 表）

新建 `delivery_channel_states`，唯一约束 `(order_id, unit_index, channel)`：

| 列 | 说明 |
| --- | --- |
| channel | `trade_text`（发货凭证）/ `chat`（私信通知） |
| status | `pending`（未送达）/ `delivered`（已送达）/ `manual_delivered`（人工结案）/ `failed`（达上限终态） |
| content_snapshot | 该单元实际发送内容（delivery_steps JSON）——原样补发快照 |
| attempt_count / next_retry_at / last_error / delivered_at | 补发调度状态机 |

需求 5.1 指出 `delivery_finalization_states` 的「订单+单元」唯一约束阻碍双通道共存。
本实现**不改该表约束**，而是让该表继续只承担「发货单元履约收尾」语义（40+ 调用点依赖
其单行/单元 upsert 与进度汇总，改动波及面大、回归风险高），双通道状态由新表按
`(order_id, unit_index, channel)` 独立记账——C2 要求的「两通道共存于同一订单同一单元、
互不覆盖」由新表唯一约束直接满足。

### 2.2 判据与 C1 存量兼容

私信缺口判据（SQL，`get_chat_notification_gap_orders`）：
**存在 `channel='trade_text'` 的履约完成记录（只有直填新链路会写）且不存在 chat 的
delivered/manual_delivered 记录**，时间窗 `chat_gap_scan_window_hours`（默认 72h）。

- 旧单通道订单（channel='auto'/'manual' 等）没有 trade_text 证据 → 天然不命中（C1 不误报）；
- 部署前已直填的存量缺口（如样本订单 5127563270248090346）→ 会被命中，
  首次处理时从 trade_text 完成记录的 `delivery_meta.data_line` **回填** chat 行与快照。

### 2.3 三种语义分离（需求 2.2）

- 平台已发货 = consign 接口成功 → `delivery_finalization_states`（channel='trade_text'）；
- 通道已送达 = 各通道状态行 delivered/manual_delivered；私信送达以 `_send_delivery_steps`
  无异常（内部已等平台 ACK）为准，未收到回执绝不标已送达（4.3 红线）；
- 履约完整 = 单元收尾完成 **且** 必需通道全部送达；私信缺口存在时订单不算双通道完整，
  巡检会持续跟踪直到结案。

### 2.4 与「发货凭证缺口」的处置分离（需求 2.5）

| 缺口 | 判定 | 处置 |
| --- | --- | --- |
| 平台已发货但直填从未成功（真·假发货） | shipped + 无任何完成记录 | 维持铁律：只用已送达内容补写凭证 / 告警人工核实，绝不新发卡密 |
| 凭证已送达、仅私信未送达 | trade_text 证据 + chat 未送达 | 可自动补发：原样快照重发，不取卡密、不匹配规则 |

补写凭证（rewrite）路径**不**产生 chat 通道行——那类订单私信状态未知（可能 App 已补发），
保持原告警人工流程，避免对两笔 10-25 事故订单自动重发。

### 2.5 幂等与防重复（需求 2.7）

- upsert 幂等红线：`delivered/manual_delivered` 的行被 CASE 保护，任何后续写入不能降级；
- 补发前必须行状态 ∈ pending/failed，且到达 next_retry_at；
- 首次发现 → 告警 + **宽限期**（默认 900s=15min，恰为扫码稳定期时长）：给运营方
  「已在 App 手工补发 → 调 manual-mark 结案」的反应窗口，避免买家收重复消息；
- 失败指数退避（base 1800s，cap 取 RISK_CONTROL.backoff_max_cap_seconds），
  默认 3 次封顶转人工告警；
- 长连接未就绪/登录退避静默期绝不发网络请求，只告警。

## 3. 改动清单

| 文件 | 改动 |
| --- | --- |
| db_manager.py | 新表 `delivery_channel_states`；`upsert_delivery_channel_state`（幂等保护+计次）；`get_delivery_channel_states`；`get_chat_notification_gap_orders`；`get_latest_delivery_gap_risk_log` 增加 event_type 参数（新增 `chat_notification_gap` 类型） |
| XianyuAutoAsync.py | 直填成功路径：先落 chat pending（含快照）→ 尽力发送 → delivered/维持 pending + chat 通道发货日志 + 即时告警（A2/A3）；`_handle_chat_notification_gap_order`（宽限/退避/上限/离线防护/人工记录结案/回填）；巡检扫描接线；`_build_chat_gap_alert_message`（2.6 全要素告警正文）；`_maybe_schedule_orders_recovery_sync`（2.8 掉线恢复一次性补拉，复用近期订单巡检，最小间隔 30min）；DELIVERY 配置 getter；UTC 时间工具 |
| reply_server.py | `POST /api/orders/{id}/deliver`：单元全部完成但私信未送达时，从快照**原样补发**私信并结案（A6）；新端点 `POST /api/orders/{id}/chat-notification/manual-mark`：只落账结案**不发消息**（App 内已手工补发场景） |
| config.py / global_config.yml | DELIVERY 段新增 9 个开关（见下），未改动任何既有 RISK_CONTROL 参数 |

### 3.1 新增配置（DELIVERY 段，均有代码默认值，不加配置也能跑）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| chat_gap_auto_resend_enabled | true | 私信缺口自动补发总开关（false=只告警） |
| chat_gap_resend_max_attempts | 3 | 达到后停止重试转人工 |
| chat_gap_resend_grace_seconds | 900 | 首次发现到首次补发的宽限 |
| chat_gap_resend_backoff_base_seconds | 1800 | 失败退避基准（指数翻倍） |
| chat_gap_scan_window_hours | 72 | 私信缺口扫描时间窗 |
| orders_recovery_sync_enabled | true | 掉线恢复后一次性补拉订单 |
| orders_recovery_sync_min_interval_seconds | 1800 | 补拉最小间隔（防重连风暴） |
| orders_recovery_sync_delay_seconds | 60 | 恢复后延迟补拉 |

### 3.2 风险日志

新增 `event_type='chat_notification_gap'`，按订单去重复用 + 结案回写，与
`delivery_gap` 互不干扰；告警仍走 `dispatch_account_notifications`（webhook→飞书），
同 key（`chat_gap:{order_id}`）冷却：常规 2h、离线/退避/达上限/宽限 12h，防刷屏（B3/B5）。

## 4. 验收映射（全部有自动化测试，tests/test_dual_channel_delivery.py）

| 编号 | 场景 | 测试 |
| --- | --- | --- |
| A1 | 直填+私信双成功 | DirectChannelBookkeepingTests::test_a1_… |
| A2 | 直填成功+私信失败：记账+chat失败日志+即时告警 | test_a2_… |
| A3 | 无私聊通道：缺口持久化+告警 | test_a3_… |
| A4 | 直填失败：不落私信账、回落不变 | test_a4_… |
| A5 | 巡检补发成功结案 | ChatGapHandlerTests::test_resend_success_… |
| A6 | 控制台人工补发 / 人工标记结案 | ReplyServerChatResendTests（4 例） |
| B1 | 巡检扫到「凭证已送达+私信未送达」 | ChannelStoreSqlTests::test_gap_scan_matches_… + ScanWiringTests |
| B2 | 长连接在线自动补发一次 | ChatGapHandlerTests::test_resend_success_… |
| B3 | 长连接不在线不盲发+告警不刷屏 | test_offline_does_not_send_… |
| B4 | 直填从未成功→不进私信缺口（保持铁律） | test_no_trade_text_evidence_… + 原 GapHandlerTests |
| B5 | 失败退避+上限+冷却不刷屏 | test_send_failure_… / test_max_attempts_… |
| C1 | 存量老订单不误报 | test_c1_legacy_orders_…（真库 SQL） |
| C2 | 双通道同单元共存互不覆盖 | test_c2_…（真库 SQL） |
| 幂等 | 已送达行永不降级 | test_delivered_row_never_downgraded（真库 SQL） |
| 2.8 | 掉线恢复一次性补拉 | RecoverySyncTests（4 例） |

测试命令：`python3 -m pytest tests/ -q`（本机基线 164 passed → 现 195 passed）。

## 5. 回滚

- 私信自动补发回滚：`DELIVERY.chat_gap_auto_resend_enabled: false`（只告警不补发）+ 重启；
- 整体回滚：还原 .py 到上一版即可——新表只增不改，旧代码不读写它，无破坏性迁移。

## 6. 部署注意

- 首次启动自动建表 `delivery_channel_states`（CREATE IF NOT EXISTS，无 ALTER、无数据迁移）；
- 存量直填缺口（如 5127563270248090346）首个巡检周期（≤5 分钟）即被扫到并告警；
  **若已在 App 手工补发，请在宽限期（默认 15 分钟）内调 manual-mark 结案**，
  否则系统会按设计原样补发私信（买家收到重复消息）；
- 验收 A1/A2 需等下一笔真实订单；B2 可用 5127563270248090346 实测（接受重复消息），
  或留给下一笔真实订单。
