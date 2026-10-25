#!/usr/bin/env python3
"""P0 灰度验证探针：直接调用 consign.dummy 接口，验证平台是否接受带 tradeText 的请求。

⚠️ 这是真实发货接口调用：会对指定订单执行「无需寄件发货」（newUnconsign=true）。
   只允许对测试账号/测试订单使用，三种情形（PRD §4.1）：
     1) 新订单(pending_ship) + 带 tradeText → 期望 SUCCESS::调用成功
     2) 已发货订单 + 带 tradeText → 记录实际错误码（决定 G5 能否自动补写凭证）
     3) 超长 tradeText(600字符, --oversize) → 确认长度上限
   绝不打印 Cookie / Token / 密码明文。

用法（在服务所在机器的项目根目录执行）:
  python3 tools/p0_trade_text_probe.py --cookie-id <账号ID> --order-id <测试订单号> [--dry-run]
  python3 tools/p0_trade_text_probe.py --cookie-id <账号ID> --order-id <测试订单号> --oversize
  python3 tools/p0_trade_text_probe.py --cookie-id <账号ID> --order-id <测试订单号> --trade-text "https://pan.quark.cn/s/xxxx\n提取码：xxxx"
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main() -> int:
    parser = argparse.ArgumentParser(description="P0 发货凭证(tradeText)灰度探针")
    parser.add_argument('--cookie-id', required=True, help='账号Cookie ID（数据库中的账号ID）')
    parser.add_argument('--order-id', required=True, help='测试订单号（⚠️ 将对其执行真实发货接口调用）')
    parser.add_argument('--trade-text', default='', help='自定义凭证正文（默认使用内置示例：链接+提取码）')
    parser.add_argument('--oversize', action='store_true', help='情形3：发送600字符超长凭证，验证平台上限')
    parser.add_argument('--max-length', type=int, default=1000, help='凭证长度硬上限（默认1000，仅影响截断）')
    parser.add_argument('--dry-run', action='store_true', help='只构造并打印 data_val，不发送请求')
    args = parser.parse_args()

    if args.oversize:
        trade_text = ('https://pan.quark.cn/s/oversizep0probe\n提取码：p0p0\n' + '长' * 600)[:args.max_length]
    else:
        trade_text = args.trade_text or 'https://pan.quark.cn/s/p0probe0001\n提取码：p0p0'

    from db_manager import db_manager
    from secure_confirm_decrypted import SecureConfirm, MAX_TRADE_TEXT_LENGTH

    account = db_manager.get_cookie_details(args.cookie_id)
    if not account or not (account.get('value') or ''):
        print(f"[P0] 账号 {args.cookie_id} 不存在或 Cookie 为空，中止", file=sys.stderr)
        return 1
    cookies_str = account['value']
    token_hint = cookies_str.split('_m_h5_tk=')[1].split(';')[0].split('_')[0] if '_m_h5_tk=' in cookies_str else ''
    print(f"[P0] 账号 {args.cookie_id} 已加载 Cookie（长度 {len(cookies_str)}），"
          f"_m_h5_tk token: {'存在(长度 ' + str(len(token_hint)) + ')' if token_hint else '缺失 ⚠️'}")

    secure_confirm = SecureConfirm(None, cookies_str, args.cookie_id)

    # 与业务链路完全一致的 data_val 构造（json.dumps + 同源签名）
    data_val = json.dumps({
        "orderId": str(args.order_id),
        "tradeText": trade_text[:args.max_length],
        "picList": [],
        "newUnconsign": True,
    }, ensure_ascii=False, separators=(',', ':'))
    print(f"[P0] data_val ({len(data_val)} 字节): {data_val[:200]}{'...' if len(data_val) > 200 else ''}")
    print(f"[P0] 模块默认长度上限 MAX_TRADE_TEXT_LENGTH={MAX_TRADE_TEXT_LENGTH}，本次探针上限={args.max_length}")

    if args.dry_run:
        print("[P0] dry-run：未发送请求")
        return 0

    print("[P0] 即将调用真实发货接口 consign.dummy（newUnconsign=true）...")
    result = await secure_confirm.auto_confirm(
        args.order_id, trade_text=trade_text, trade_text_max_length=args.max_length)

    print(f"[P0] 原始返回: {json.dumps(result, ensure_ascii=False)}")
    if result.get('success'):
        print("[P0] ✅ 情形通过：平台接受带 tradeText 的发货请求（G1/G2 主路径可行）")
        return 0
    error_text = str(result.get('error') or '')
    if 'ORDER_STATUS_ERROR' in error_text:
        print("[P0] ⚠️ 情形2结论：已发货订单不允许补写凭证（G5 自动补发降级为『仅告警』，"
              "把 global_config.yml 的 RISK_CONTROL.delivery_gap_auto_retry 置 false 即可）")
    elif 'SESSION_EXPIRED' in error_text or result.get('session_expired'):
        print("[P0] ⚠️ Session 过期：请先扫码登录刷新 Cookie 再测")
    elif 'SIGN' in error_text.upper() or 'ILLEGAL' in error_text.upper():
        print("[P0] ❌ 签名类错误：sign 与 data_val 不同源，立即回滚（R3）")
    else:
        print("[P0] ❌ 其他错误：记录错误码并反馈，未通过前不要放量")
    return 2


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
