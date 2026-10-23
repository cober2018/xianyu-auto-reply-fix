# 需求规格说明书 (PRD / RFC)

- **文档名称**：闲鱼自动化运维系统 —— 单一凭证源收口与扫码登录两路打通自愈架构重构
- **提交方**：闲鱼运维 Agent (xianyu-ops)
- **接收方**：后端开发 Agent (Developer Agent)
- **适用仓库**：cober2018/xianyu-auto-reply-fix
- **涉及模块**：reply_server.py, XianyuAutoAsync.py, utils/qr_login.py, cookie_manager.py, db_manager.py
- **版本号**：v2.1.0-RFC

## 一、背景与问题现状深度复盘

### 1.1 业务背景

系统承载全自动虚拟商品履约：

- **HTTP/REST 网关**：负责发品、编辑、查订单、调用无需物流发货（dummy consign）。
- **IM/WebSocket 长连接通道**：负责与买家即时通讯、自动回复以及在买家付款后私信下发网盘卡券。

根据业务铁律（强原子性履约），系统只有在确认买家私信送达（收到阿里服务端 ACK 成功回执）后，才允许调用平台确认发货接口。

### 1.2 现存核心痛点与缺陷分析

1. **两套通道凭证分裂，更新不同步**：阿里对私信外链风控严苛，当 IM 通道被风控拒收（code=400）或 Token 过期时，系统虽然能够通过 qr-generate 生成二维码由号主在手机 App 上扫码成功；但扫码成功后，拉取到的新 Cookie 仅部分入库，后台常驻的长连接任务未能无缝热重载，依然拿着老旧的内存会话在重连，导致扫码后依然持续报 code=400。
2. **类变量退避锁阻断自愈（“扫了码还是不通”的根因）**：在 XianyuAutoAsync.py 中，一旦触发了 pureCaptcha/RGV587 或登录失败，类变量 `_password_login_failure_backoff` 会被锁死 3600 秒或更长；即使数据库已经写入了用户刚刚扫码授权的最新有效凭证，长连接实例在启动时仍然直接读取该内存退避标记，判为“冷静期退避中”，直接跳过初始化，导致扫码成功却无法上线，必须物理重启服务。
3. **服务重启导致管理后台 Session 丢失（401 级联报错）**：reply_server.py 中的 SESSION_TOKENS 采用模块级内存 dict 存储；为了使新 Cookie 生效而不得不重启 Python 进程，一旦重启，管理员的登录态全部丢失，导致运维调用所有 REST API 瞬间返回 401 未授权访问。
4. **探针连接自激放大风控**：在主连接断开或发信受阻时，补偿逻辑会尝试临时建立独立的 WebSocket 重新连接；在已触发频率限制（RGV587）时，反复建立新连接调 mtop.taobao.idlemessage.pc.login.token 会被判定为恶意请求，在 risk_control_logs 中不断堆积硬拦截，加深封控等级。

## 二、架构重构总体目标

构建“**单一凭证源（Single Source of Truth）+ 扫码自愈一键打通两路 + 实例安全热重载**”机制：

1. **无缝自愈**：一旦长连接私信失效，用户在手机闲鱼 App 扫码确认后，系统自动在进程内无缝热重载最新凭证，无需重启进程，管理后台 Token 不失效。
2. **退避状态瞬时清零**：扫码成功作为最高优先级人工授权事件，必须原子化清空当前账号所有的错误计数、风控熔断退避锁（包括 `_password_login_failure_backoff` 和 hard_block）。
3. **两路强绑定打通**：扫码获取的 Cookie 同时激活 HTTP 业务与 IM 长连接，扫码成功后自动触发积压待发货订单的补发。

## 三、详细需求与改动规范

### 1. 凭证单一源收口与落库完整性（utils/qr_login.py & db_manager.py）

用户在手机 App 扫码并在服务端确认后，必须通过现有的无头浏览器稳定化逻辑（https://www.goofish.com/im）获取包含完整字段的真实 Cookie 字符串（特别是 `_m_h5_tk`, `_m_h5_tk_enc`, `cookie2`, `sgcookie`, `unb`, `t`, `cna`）；必须原子写入数据库 cookies 表，并立即将账号的 `qr_login_grace_until` 设置为当前时间 + 900 秒（15 分钟稳定期防主动探针风控），同时清空 status_note。

### 2. 扫码成功时彻底清除内存退避锁（reply_server.py & XianyuAutoAsync.py）

在 reply_server.py 的 /qr-login/check/{session_id} 捕获到扫码成功处理逻辑中，必须显式调用清理方法：

```python
# 彻底清除所有进程内历史风控退避与失败计数
xianyu_instance = cookie_manager.manager.get_xianyu_instance(cookie_id)
if xianyu_instance:
    xianyu_instance.clear_all_login_backoffs()
XianyuLive.clear_class_level_backoffs(cookie_id)
```

在 XianyuAutoAsync.py 中新增/完善 `clear_class_level_backoffs(cookie_id)` 类方法：

- 将 `_password_login_failure_backoff` 中该 cookie_id 的条目彻底移除；
- 将 `last_token_refresh_status` 重置为 None；
- 将 `connection_failures`、`init_auth_failures` 归零。

### 3. 长连接实例优雅热重载（Hot Reload without Restart）

- 严禁通过退出整个 Python 进程（如 sys.exit() 或 kill）来重载 Cookie；
- 扫码落库并清除退避后，cookie_manager 必须安全调度实例刷新：取消并等待当前长连接循环（XianyuLive.main 任务）；主动关闭当前失效的 WebSocket 连接；重新读取数据库最新的 Cookie 字符串，实例化全新的 XianyuLive 实例；启动新的后台循环并注册到全局字典。

核心效益：Uvicorn 与 reply_server.py 常驻不中断，SESSION_TOKENS 完好无损，前端和调用方零感知，不产生 401 未授权访问。

### 4. 履约发货等待与优雅补偿

- 待发货订单（pending_ship）在长连接处于风控熔断（hard_block 或 code=400）期间，看门狗程序应自动识别账号当前为“等待人工扫码中”，进入低频静默等待（每 5~10 分钟仅做只读检查，严禁高频唤起新连接打 Token 接口），防止产生新的 RGV587 挤爆拦截；
- 一旦扫码热重载完成（connection_state == 'connected' 且 has_current_token == True），系统触发一次高优先级的 recent_order_watchdog 巡检，使用全新长连接自动将积压的订单发货并点确认发货。

## 四、验证验收标准 (Test & Acceptance Criteria)

| 序号 | 验证场景 | 预期结果 |
| --- | --- | --- |
| AC-1 | 扫码成功落库 | 手机 App 扫码确认后，cookies 表中 value 字段被更新为最新密文，包含完整的 _m_h5_tk 字段，且 qr_login_grace_until 刷新为未来时间。 |
| AC-2 | 内存退避即时清除 | 扫码成功后，调用 GET /cookies/{cid}/runtime-status，token_refresh_status 不再显示 hard_block，退避倒计时归零。 |
| AC-3 | 无重启热切换 | 扫码落库后，后台服务进程 PID 不变，使用既有管理员 token 请求 /api/orders 保持 HTTP 200（绝不报 401 未授权访问）。 |
| AC-4 | 长连接私聊恢复 | 新长连接建立后，订单补发走 WebSocket 发送并成功捕获阿里服务端 code: 200 的 ACK 回包，不报 MessageSendAckTimeoutError。 |
| AC-5 | 订单履约强原子闭环 | 私聊送达后，系统自动调用 dummy 确认发货，订单状态从 pending_ship 变更为 shipped，履约日志记录为 status: success。 |

## 五、实现落点（开发 Agent 备注）

- `XianyuAutoAsync.py`：
  - 类方法 `clear_class_level_backoffs(cookie_id)`、`request_post_qr_delivery_patrol(cookie_id)`、`consume_post_qr_delivery_patrol_request(cookie_id)`；
  - 实例方法 `clear_all_login_backoffs()`（含唤醒事件 `_login_backoff_wake_event`）；
  - `_get_manual_qr_wait_seconds()`：退避期间返回 300~600 秒低频等待；`main()` 重连前据此静默等待，扫码清锁立即唤醒；
  - `_run_post_qr_delivery_patrol()`：连接就绪后消费巡检请求，进入履约优先，逐单走 `_auto_deliver_recovered_pending_order` 原子链路；
  - 消息循环内超时订单补偿与近期订单巡检在退避期间暂停（退避巡检会打平台详情接口/拉无头浏览器）。
- `db_manager.py`：`get_pending_ship_orders(cookie_id, limit)` 只读查询 pending_ship/pending_delivery 订单。
- `reply_server.py`：`_clear_login_backoffs_for_qr_success(account_id)` 统一挂载在标准扫码、轻量扫码（共用的 process_qr_login_cookies）、降级保存与 /qr-login/refresh-cookies 四条成功路径上，顺序为“先清退避→再热重载→登记补发巡检”，并清空 status_note、恢复被风控自动停用的账号。
- 单元测试：`tests/test_qr_self_healing.py`。
