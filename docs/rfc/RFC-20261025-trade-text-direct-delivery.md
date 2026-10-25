# RFC-20261025：发货凭证直填（tradeText）+ 履约异常自愈 实现记录

对应 PRD：《闲鱼数字化商品「发货凭证直填」链路改造 + 履约异常自愈 v1.0（2026-10-25）》。
事故背景：2026-10-25 两单假发货（5127830820177000639 / 5127889249199170147）——私聊 ACK 超时
导致发货链路失败，平台侧订单自行流转为 shipped，看门狗被 pending_ship 硬闸门锁死。

## 一、改造点对照

| 改造点 | 文件 | 说明 |
| --- | --- | --- |
| D1 consign 接口支持 tradeText | `secure_confirm_decrypted.py` | `auto_confirm(order_id, item_id, retry_count, trade_text='', trade_text_max_length=None)`；data_val 用 `json.dumps(ensure_ascii=False, separators=(',',':'))` 紧凑序列化，`generate_sign(t, token, data_val)` 与最终 data_val 逐字节同源；两处递归重试均透传 trade_text。空凭证与旧版仅差 orderId 后一个空格（JSON 语义等价，签名同源） |
| D1 包装器透传 | `XianyuAutoAsync.py auto_confirm()` | 透传 trade_text / trade_text_max_length 给 SecureConfirm |
| D2 凭证正文构造 | `XianyuAutoAsync.py` | `extract_first_url` / `extract_extraction_code`（标签式+链接尾随短码，英文单词误报过滤）/ `build_trade_text_body`（主链接+提取码+≤60字白名单说明，禁词说明整体丢弃）/ `build_order_trade_text`（多单元逐块拼接，任一单元无链接则整单回落） |
| D3 双通道直填 | `XianyuAutoAsync.py _deliver_via_trade_text_direct()` | 返回 True/False/None 三态：True=平台凭证发货成功并已逐单元收尾（`skip_confirm=True` 消费卡密）、私聊降级为 best-effort 补充告知；False=直填失败（调用方按 `trade_text_fallback_to_chat` 回落旧私聊链路）；None=不适用（开关关/无链接/有在途单元/确认冷却中/账号未开自动确认）。幂等：`confirmed_orders` 冷却 + 只处理无在途单元的全新订单 |
| D3 接线×4 | `_handle_auto_delivery`（统一实时）、`_handle_simple_message_auto_delivery`（简化消息）、`_send_recovered_delivery_without_sid`（无sid补偿=事故路径）、`reply_server /api/orders/{oid}/deliver`（手动发货） | 全部"直填优先，失败回落"；直填失败且 `trade_text_fallback_to_chat=false` 时释放预占并记失败 |
| D4 缺口巡检 | `XianyuAutoAsync.py _delivery_consistency_patrol_loop/_scan_delivery_gap_orders/_handle_delivery_gap_order` + `db_manager.get_shipped_orders_without_finalization()` | 进程级低频巡检（默认300s，与WS状态无关）：LEFT JOIN delivery_finalization_states 识别 24h 内 shipped-无-finalized 订单 → 写 risk_control_logs(event_type='delivery_gap') → 告警 + （仅当存在已送达记录时）用已送达内容补写凭证。**铁律：绝不新发卡密**。附带 pending_ship 滞留>15min 告警 |
| G5 看门狗闸门 | `_auto_deliver_recovered_pending_order` | shipped 订单不再被硬闸门拒绝，转 `_handle_delivery_gap_order`（补写凭证/告警人工，不重发卡密） |
| D5 通知冷却 | `XianyuAutoAsync.py send_token_refresh_notification` | 通知发送失败也进入同档冷却（原先"不进入冷却"导致每10分钟刷屏）；新增 `_send_ops_alert`（每key冷却，失败也进冷却）用于发货失败/履约缺口/滞留告警 |
| 配置 | `config.py` + `global_config.yml` | 新增 `DELIVERY` 节（trade_text_enabled/trade_text_max_length/trade_text_fallback_to_chat/chat_notify_best_effort）与 `RISK_CONTROL` 四键（delivery_gap_scan_interval/delivery_gap_auto_retry/delivery_gap_pending_ship_alert_minutes/delivery_failure_alert_enabled） |
| P0 探针 | `tools/p0_trade_text_probe.py` | 直接打真实 consign.dummy 验证三种情形（新订单/已发货补写/超长），--dry-run 只构造不发送，绝不打印 Cookie/Token |

## 二、防重发与原子性保障（Bug-1/bug-2 铁律延续）

1. 直填只在「全部单元均为 remaining」的全新订单上触发；任何 sent/finalized 在途单元 → 走 Partial 强幂等链路；
2. 多单元订单任一单元提取不出链接（图片卡券等）→ 整单回落旧私聊链路，绝不半发；
3. 缺口订单只允许补写「已送达记录里的内容」的凭证，尝试冷却 30 分钟，`ORDER_STATUS_ERROR` → 升级人工告警；
4. 登录退避静默期（hard_block 等）巡检只做只读检测+告警，零网络请求。

## 三、灰度与回滚

- P0：`python3 tools/p0_trade_text_probe.py --cookie-id <id> --order-id <测试单> [--dry-run|--oversize]`
- 回滚：`global_config.yml` 的 `DELIVERY.trade_text_enabled: false` → 完全回落旧私聊链路（A6），改配置后需重启服务
- G5 补写不可行时（P0 情形2 得到 ORDER_STATUS_ERROR）：`RISK_CONTROL.delivery_gap_auto_retry: false` → 降级为仅告警

## 四、测试

`tests/test_trade_text_delivery.py`（32 用例）：data_val 紧凑序列化与签名同源、空凭证语义等价、
重试透传、截断；链接/提取码提取与禁词剔除；断连直填成功（私聊失败被容忍）、失败回落、
在途/图片/开关/冷却不适用；缺口补写/ORDER_STATUS_ERROR 升级/退避静默/尝试冷却；
看门狗 shipped 路由；告警与 Token 通知失败进冷却；缺口扫描与滞留告警。
全仓 17 个测试模块全绿。
