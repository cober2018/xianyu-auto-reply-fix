---
name: xianyu-auto-reply
description: 操控本机运行的闲鱼自动回复服务(xianyu-auto-reply,FastAPI,默认 http://127.0.0.1:8090):查询账号与会话、发送消息、管理自动回复,以及探查风控告警并执行恢复链路(自动重登/账密重登/扫码重登)。当用户提到闲鱼账号状态、风控、重新登录、自动回复配置、消息查询或发送时使用。
---

# 闲鱼自动回复服务操控

## 服务定位与前置检查

- 默认地址 `http://127.0.0.1:8090`,可用环境变量 `XIANYU_BASE_URL` 或 `--base-url` 覆盖。
- 动手前先确认服务存活:`scripts/xianyu_api.py health`(无需登录)。连不上时提示用户检查服务(Docker 部署或本地 `python Start.py`)。
- 全部端点的权威定义在服务的 `/openapi.json`(FastAPI 自动生成);本文件只列高频操作,没覆盖的先查 openapi 再调用。

## 认证

1. 首次使用运行 `scripts/xianyu_api.py login`(缺参数时会交互询问;也可 `--username/--password` 或环境变量传入)。
2. 凭证(含 token)保存在 `~/.config/xianyu-skill/credentials.json`,权限 0600。token 过期时脚本会用已存密码自动重登一次。
3. **绝对不要**在任何输出、日志、命令回显中打印 token 或密码。
4. 遇到 403 说明当前用户无权操作该账号(账号归属校验),不要重试,向用户说明。

## 辅助脚本命令速查

所有命令输出 JSON(`http_status` + `body`),退出码:0 成功 / 1 HTTP 错误 / 2 连接失败 / 3 未登录。

| 命令 | 说明 |
|---|---|
| `login` | 登录并保存凭证 |
| `health` | 服务健康检查 |
| `accounts` | `GET /cookies/details` 全部账号及状态(风控探查入口) |
| `status <cookie_id>` | `GET /cookies/{cid}/runtime-status` 单账号运行态 |
| `qr-generate` | 生成扫码登录二维码(返回含 `qr_code_url`) |
| `qr-check <sid>` | 轮询扫码结果 |
| `qr-refresh` | 二维码方式刷新 Cookie |
| `qr-cooldown <cid>` | 查询该账号扫码重登冷却剩余时间 |
| `qr-reset-cooldown <cid>` | 重置冷却(**需用户明确同意**) |
| `pwd-login <cookie_id>` | 用已保存的账密自动重登(refresh_mode) |
| `pwd-check <sid>` / `pwd-cancel <sid>` | 轮询/取消账密登录任务 |
| `get/post/put/delete <path> [json]` | 通用请求,调用其他任意端点 |

## 高频操作映射

| 操作 | 端点 |
|---|---|
| 全部账号列表与状态 | `GET /cookies/details` |
| 单账号运行态(保活/连接诊断) | `GET /cookies/{cid}/runtime-status` |
| 会话消息历史 | `GET /cookies/{cid}/conversations/{conversation_id}/history` |
| 账号商品列表 | `GET /items/cookie/{cookie_id}` |
| 会话保活 | `POST /cookies/{cid}/session-keepalive` |
| 主动发消息给买家 | `POST /send-message`,body 需 `api_key`(在系统设置中配置;没有就先问用户) |
| 触发自动回复匹配 | `POST /xianyu/reply` |
| 消息通知配置 | `GET/POST /message-notifications/{cid}` |

## 风控处置 SOP

典型场景:账号已登录但触发风控,需要刷新或重新登录。按以下流程执行,**不要跳步**。

### 1. 探查(确认风控存在与类型)

1. `accounts` 查看所有账号:被风控的账号会被服务自动暂停并带保护状态标记(v1.9.3+)。
2. `status <cid>` 看运行态:连接是否断开、保活是否失败。
3. 需要细节时读服务器 `logs/` 目录日志或数据库 `risk_control_logs` 表,区分类型:
   - **轻量风控**(token 失效/自动滑块):服务 v2.0.x 通常会自动处理;
   - **登录态失效**:需要重登;
   - **严重风控**:只能等冷却或人工扫码。

### 2. 决策树

| 情况 | 动作 |
|---|---|
| 服务显示自动处理中(滑块/token 刷新) | 等待 2~5 分钟,轮询 `status` 确认恢复,**不要干预** |
| 账号保存过账密 | `pwd-login <cid>` → 每 10 秒 `pwd-check <sid>`,直到成功或失败 |
| 无账密或账密登录失败 | `qr-generate` → **把返回的 `qr_code_url` 二维码展示给用户**请其用闲鱼 App 扫码 → 每 5 秒 `qr-check <sid>` |
| 扫码处于冷却期 | `qr-cooldown <cid>` 查剩余时间,告知用户等待;`qr-reset-cooldown` 仅在用户明确要求时使用 |

### 3. 执行纪律(重要)

- **风控期间严禁高频重试**——重复登录尝试会加重风控。所有轮询间隔 ≥ 5~10 秒。
- 任一链路连续失败 2 次即停止,升级给用户决定,不要自行换链路反复尝试。
- 账密登录可能触发滑块验证或人脸认证:滑块服务可自动处理(轮询等待);**人脸/扫码必须人工**,agent 的职责是把二维码/提示清晰呈现给用户并持续轮询结果。

### 4. 验证恢复

重登成功后:① `status <cid>` 运行态恢复正常;② 必要时 `session-keepalive` 主动保活;③ 观察会话历史有新消息流动。确认后再向用户汇报。

### 5. 汇报格式

向用户报告:风控类型、走了哪条恢复链路、人工参与了什么、当前账号状态、后续建议(如保存账密以便下次自动重登)。

## 安全红线

- **删除类操作**(`DELETE` 请求、删除账号/关键词/卡券等)必须先向用户确认再执行。
- 给真实买家发送消息前必须经用户确认;群发/批量操作一律先展示计划再执行。
- 不修改全局配置(`global_config.yml`、系统设置)除非用户明确要求。
- 凭证文件不得复制到别处,不得在对话中回显。

## 故障排查

- 连接失败(退出码 2):服务未启动或端口不对;本地部署检查 `python Start.py`,Docker 部署检查容器状态与端口映射。
- 退出码 3:运行 `login`。
- 某端点 404:版本差异,以 `/openapi.json` 为准。
