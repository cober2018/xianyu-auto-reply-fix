"""发货凭证直填（tradeText）+ 履约一致性巡检 单元测试。

覆盖：
- SecureConfirm.auto_confirm：data_val 与签名同源、空凭证与旧版逐字节一致、重试保留 trade_text
- 凭证正文构造：链接/提取码提取、禁词剔除、截断、开关关闭
- _deliver_via_trade_text_direct：断连发货成功、失败回落、在途订单/图片卡券不适用
- _handle_delivery_gap_order：缺口补写、ORDER_STATUS_ERROR 升级人工、退避静默、绝不新发卡密
- 看门狗闸门：shipped 订单转缺口处理
- 运营告警与 Token 通知冷却：发送失败也进冷却
"""
import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import db_manager as dbm
import XianyuAutoAsync
from XianyuAutoAsync import XianyuLive


def _direct_live(cookie_id='trade-unit'):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = cookie_id
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 600
    live._ops_alert_last_sent = {}
    live._delivery_gap_attempt_last = {}
    live.user_id = 1

    live.is_auto_confirm_enabled = lambda: True
    live._get_order_expected_delivery_quantity = lambda order_id: 1
    live._summarize_delivery_progress = lambda order_id, expected_quantity=1: {
        'order_id': order_id,
        'expected_quantity': expected_quantity,
        'aggregate_status': 'pending_ship',
        'finalized_unit_indexes': [],
        'pending_finalize_unit_indexes': [],
        'remaining_unit_indexes': list(range(1, expected_quantity + 1)),
    }
    live._mark_data_reservation_sent_if_needed = lambda meta: True
    live._finalize_delivery_after_send = mock.AsyncMock(return_value={'success': True})
    live._persist_delivery_finalization_state = mock.MagicMock(return_value=True)
    live._record_delivery_log = mock.MagicMock()
    live._record_chat_channel_state = mock.MagicMock()
    live._sync_order_delivery_progress = mock.MagicMock()
    live._activate_delivery_lock = mock.MagicMock()
    live.auto_confirm = mock.AsyncMock(return_value={'success': True, 'order_id': 'po1'})
    live._send_ops_alert = mock.AsyncMock(return_value=True)
    live._get_manual_qr_wait_seconds = lambda: 0
    return live


def _prepared_unit(content, unit_index=1, meta=None):
    return {
        'unit_index': unit_index,
        'delivery_steps': [{'type': 'text', 'content': content}],
        'rule_meta': meta or {'success': True, 'rule_id': 7, 'card_type': 'text',
                              'delivery_unit_index': unit_index},
    }


class SecureConfirmTradeTextTests(unittest.TestCase):
    def _run_confirm(self, responses, trade_text='', max_length=None):
        import secure_confirm_decrypted as sc
        state = {'requests': [], 'responses': list(responses)}
        signs = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = {}

            async def json(self):
                return self._payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakePost:
            def post(self, url, params=None, data=None, proxy=None):
                state['requests'].append({
                    'url': url,
                    'params': dict(params or {}),
                    'data': dict(data or {}),
                })
                return FakeResponse(state['responses'].pop(0))

        class FakeSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return FakePost()

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(sc.aiohttp, 'ClientSession', FakeSession), \
                mock.patch.object(sc, 'generate_sign',
                                  side_effect=lambda t, token, data_val: signs.append((t, token, data_val)) or 'fakesign'):
            confirm = sc.SecureConfirm(None, 'foo=1; _m_h5_tk=abc123_test', 'probe')
            result = asyncio.run(confirm.auto_confirm(
                'PO123', trade_text=trade_text, trade_text_max_length=max_length))
        return result, state['requests'], signs

    def test_empty_trade_text_semantically_identical_and_compact(self):
        result, requests, signs = self._run_confirm([{'ret': ['SUCCESS::调用成功']}])
        self.assertTrue(result.get('success'))
        self.assertEqual(len(requests), 1)
        data_val = requests[0]['data']['data']
        legacy = '{"orderId":"PO123", "tradeText":"","picList":[],"newUnconsign":true}'
        # JSON 语义等价（服务端按 JSON 解析），且新格式为无空格紧凑 JSON；签名同源
        self.assertEqual(json.loads(data_val), json.loads(legacy))
        self.assertEqual(json.loads(data_val), {
            'orderId': 'PO123', 'tradeText': '', 'picList': [], 'newUnconsign': True})
        self.assertNotIn(', ', data_val)
        self.assertEqual(signs[0][2], data_val)

    def test_trade_text_in_payload_and_sign_uses_same_data_val(self):
        text = 'https://pan.quark.cn/s/abc\n提取码：8k2p'
        result, requests, signs = self._run_confirm([{'ret': ['SUCCESS::调用成功']}], trade_text=text)
        self.assertTrue(result.get('success'))
        data_val = requests[0]['data']['data']
        payload = json.loads(data_val)
        self.assertEqual(payload['tradeText'], text)
        self.assertEqual(payload['newUnconsign'], True)
        self.assertEqual(payload['picList'], [])
        self.assertEqual(signs[0][2], data_val)

    def test_retry_preserves_trade_text(self):
        text = 'https://pan.baidu.com/s/xyz 密码：8k2p'
        responses = [
            {'ret': ['FAIL_SYS_UNKNOWN::暂时失败']},
            {'ret': ['SUCCESS::调用成功']},
        ]
        result, requests, signs = self._run_confirm(responses, trade_text=text)
        self.assertTrue(result.get('success'))
        self.assertEqual(len(requests), 2)
        self.assertIn('tradeText":"https://pan.baidu.com/s/xyz', requests[0]['data']['data'])
        self.assertEqual(requests[0]['data']['data'], requests[1]['data']['data'])
        self.assertEqual(signs[0][2], requests[0]['data']['data'])
        self.assertEqual(signs[1][2], requests[1]['data']['data'])

    def test_trade_text_truncated_by_max_length(self):
        long_text = 'https://pan.quark.cn/s/abc\n' + '长' * 300
        result, requests, _ = self._run_confirm(
            [{'ret': ['SUCCESS::调用成功']}], trade_text=long_text, max_length=50)
        self.assertTrue(result.get('success'))
        self.assertLessEqual(len(json.loads(requests[0]['data']['data'])['tradeText']), 50)


    def test_order_already_delivery_is_terminal_no_retry(self):
        """P0灰度实测错误码：平台已发货 → 终态，单次请求即返回，绝不重试。"""
        result, requests, _ = self._run_confirm(
            [{'ret': ['ORDER_ALREADY_DELIVERY::已发货成功，请刷新页面~']}],
            trade_text='https://pan.quark.cn/s/abc\n提取码：8k2p')
        self.assertFalse(result.get('success'))
        self.assertTrue(result.get('order_already_delivered'))
        self.assertTrue(result.get('order_status_error'))
        self.assertTrue(result.get('stop_confirm_retry'))
        self.assertEqual(len(requests), 1)


class NonRetryableClassifierTests(unittest.TestCase):
    def test_order_already_delivery_text_is_non_retryable(self):
        live = _direct_live('cls-unit')
        self.assertTrue(live._is_non_retryable_platform_confirm_error(
            '自动确认发货失败: ORDER_ALREADY_DELIVERY::已发货成功，请刷新页面~'))

    def test_flagged_result_is_non_retryable(self):
        live = _direct_live('cls-unit')
        self.assertTrue(live._is_non_retryable_platform_confirm_error(
            '业务错误', {'order_already_delivered': True}))


class TradeTextBuilderTests(unittest.TestCase):
    def test_baidu_link_and_code(self):
        body = XianyuLive.build_trade_text_body('链接: https://pan.baidu.com/s/1aBcDe 提取码: 8k2p')
        self.assertEqual(body, 'https://pan.baidu.com/s/1aBcDe\n提取码：8k2p')

    def test_unlabeled_code_after_url(self):
        body = XianyuLive.build_trade_text_body('https://pan.baidu.com/s/1aBcDe 8k2p')
        self.assertEqual(body, 'https://pan.baidu.com/s/1aBcDe\n提取码：8k2p')

    def test_english_word_not_mistaken_as_code(self):
        body = XianyuLive.build_trade_text_body('the file is here https://pan.baidu.com/s/abc')
        self.assertEqual(body, 'https://pan.baidu.com/s/abc')

    def test_forbidden_description_dropped(self):
        body = XianyuLive.build_trade_text_body(
            'https://pan.baidu.com/s/1aBcDe 提取码: 8k2p', card_description='防屏蔽备用通道，请回复1')
        self.assertEqual(body, 'https://pan.baidu.com/s/1aBcDe\n提取码：8k2p')

    def test_clean_description_kept_and_truncated(self):
        body = XianyuLive.build_trade_text_body(
            'https://pan.baidu.com/s/1aBcDe 提取码: 8k2p', card_description='好' * 100)
        self.assertTrue(body.endswith('好' * 60))
        self.assertEqual(len(body.split('\n')), 3)

    def test_no_link_returns_empty(self):
        self.assertEqual(XianyuLive.build_trade_text_body('直接发卡密 ABC-123'), '')
        self.assertEqual(XianyuLive.build_trade_text_body('__IMAGE_SEND__9|http://x/y.png'), '')

    def test_multiline_length_limit(self):
        payload = 'https://pan.baidu.com/s/1aBcDe 提取码: 8k2p\n说明文字'
        body = XianyuLive.build_trade_text_body(payload, max_length=30)
        self.assertLessEqual(len(body), 30)

    def test_order_level_requires_all_units_extractable(self):
        ok = 'https://pan.baidu.com/s/a1 提取码: 1111'
        self.assertEqual(XianyuLive.build_order_trade_text([ok, ok]).count('https://'), 1)  # 同卡券去重
        self.assertEqual(XianyuLive.build_order_trade_text([ok, '纯文本卡密']), '')
        other = 'https://pan.baidu.com/s/a2 提取码: 2222'
        self.assertEqual(XianyuLive.build_order_trade_text([ok, other]).count('https://'), 2)  # 不同链接都保留

    def test_instance_builder_disabled_by_config(self):
        live = _direct_live('cfg-unit')
        with mock.patch.dict(XianyuAutoAsync.DELIVERY, {'trade_text_enabled': False}):
            self.assertEqual(live._build_trade_text('https://pan.baidu.com/s/a 提取码: 1111'), '')
        with mock.patch.dict(XianyuAutoAsync.DELIVERY, {'trade_text_enabled': True}):
            self.assertIn('https://pan.baidu.com/s/a', live._build_trade_text('https://pan.baidu.com/s/a 提取码: 1111'))


class DirectDeliveryTests(unittest.TestCase):
    def test_success_without_websocket_chat_failure_tolerated(self):
        live = _direct_live()
        # 真实走 _send_chat_supplement_after_trade_text：私聊发送抛错必须被吞（不影响履约）
        live._send_delivery_steps = mock.AsyncMock(
            side_effect=RuntimeError('连接在收到回执前关闭'))

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=object(), chat_id='cid1', user_url='http://u', source='test')

        self.assertTrue(asyncio.run(run()))
        live.auto_confirm.assert_awaited_once()
        args, kwargs = live.auto_confirm.call_args
        self.assertEqual(args[0], 'po1')
        self.assertIn('https://pan.baidu.com/s/a1', kwargs.get('trade_text'))
        self.assertIn('提取码：8k2p', kwargs.get('trade_text'))
        # 平台凭证成功 → 收尾不触发平台确认（skip_confirm=True）
        live._finalize_delivery_after_send.assert_awaited_once()
        self.assertTrue(live._finalize_delivery_after_send.call_args.kwargs.get('skip_confirm'))
        statuses = [c.kwargs.get('status') for c in live._persist_delivery_finalization_state.call_args_list]
        self.assertIn('finalized', statuses)
        # 私聊补充告知已尝试且失败被容忍
        live._send_delivery_steps.assert_awaited_once()

    def test_platform_confirm_failure_returns_false_for_fallback(self):
        live = _direct_live()
        live.auto_confirm = mock.AsyncMock(return_value={'error': 'FAIL_SYS_SESSION_EXPIRED::Session过期'})

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                source='test')

        self.assertFalse(asyncio.run(run()))
        live._finalize_delivery_after_send.assert_not_awaited()

    def test_inflight_units_skip_direct(self):
        live = _direct_live()
        live._summarize_delivery_progress = lambda order_id, expected_quantity=1: {
            'finalized_unit_indexes': [],
            'pending_finalize_unit_indexes': [1],
            'remaining_unit_indexes': [],
        }

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1')], source='test')

        self.assertIsNone(asyncio.run(run()))
        live.auto_confirm.assert_not_awaited()

    def test_image_unit_not_applicable(self):
        live = _direct_live()

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[{'unit_index': 1,
                                 'delivery_steps': [{'type': 'image', 'content': '__IMAGE_SEND__1|u'}],
                                 'rule_meta': {'card_type': 'image'}}],
                source='test')

        self.assertIsNone(asyncio.run(run()))
        live.auto_confirm.assert_not_awaited()

    def test_disabled_by_config(self):
        live = _direct_live()

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1')], source='test')

        with mock.patch.dict(XianyuAutoAsync.DELIVERY, {'trade_text_enabled': False}):
            self.assertIsNone(asyncio.run(run()))
        live.auto_confirm.assert_not_awaited()

    def test_confirmed_cooldown_skip(self):
        import time as _time
        live = _direct_live()
        live.confirmed_orders['po1'] = _time.time()

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1')], source='test')

        self.assertIsNone(asyncio.run(run()))
        live.auto_confirm.assert_not_awaited()

    def test_multi_unit_all_links_in_one_trade_text(self):
        live = _direct_live()
        live._get_order_expected_delivery_quantity = lambda order_id: 2
        live._finalize_delivery_after_send = mock.AsyncMock(return_value={'success': True})

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 1111', 1),
                                _prepared_unit('https://pan.baidu.com/s/a2 提取码: 2222', 2)],
                source='test')

        self.assertTrue(asyncio.run(run()))
        trade_text = live.auto_confirm.call_args.kwargs.get('trade_text')
        self.assertIn('/s/a1', trade_text)
        self.assertIn('/s/a2', trade_text)
        self.assertEqual(live._finalize_delivery_after_send.await_count, 2)


class GapHandlerTests(unittest.TestCase):
    ORDER = {'order_id': 'poG', 'item_id': 'itG', 'buyer_id': 'bG', 'buyer_nick': '买家G',
             'sid': 'sidG@goofish', 'order_status': 'shipped', 'updated_at': '2026-10-25 12:00:00'}

    def setUp(self):
        # 缺口处理器直连 db_manager 单例：统一挡住风险日志读写，避免污染真实库并保证确定性
        for target, ret in (
            ('add_risk_control_log', None),
            ('get_latest_delivery_gap_risk_log', None),
            ('update_risk_control_log', True),
        ):
            patcher = mock.patch.object(dbm.db_manager, target, return_value=ret)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _gap_live(self, sent_units=(), sent_metas=None):
        live = _direct_live('gap-unit')
        live._get_order_expected_delivery_quantity = lambda order_id: 2
        live._summarize_delivery_progress = lambda order_id, expected_quantity=2: {
            'finalized_unit_indexes': [],
            'pending_finalize_unit_indexes': list(sent_units),
            'remaining_unit_indexes': [],
        }
        metas = sent_metas or {}
        live._get_pending_delivery_finalization_meta = (
            lambda order_id, unit: metas.get(unit) or {
                'success': True, 'delivery_unit_index': unit,
                'content': f'https://pan.baidu.com/s/g{unit} 提取码: 000{unit}',
            }
        )
        return live

    def test_finalized_units_not_a_gap(self):
        live = self._gap_live()
        live._summarize_delivery_progress = lambda order_id, expected_quantity=2: {
            'finalized_unit_indexes': [1], 'pending_finalize_unit_indexes': [], 'remaining_unit_indexes': [2],
        }

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER)))
        self.assertFalse(result)
        live.auto_confirm.assert_not_awaited()
        live._send_ops_alert.assert_not_awaited()

    def test_rewrite_credential_from_sent_records_success(self):
        live = self._gap_live(sent_units=[1, 2])

        # 绝不新发卡密：不应调用 _auto_delivery（重新匹配规则/预占新卡密）
        live._auto_delivery = mock.AsyncMock()
        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertTrue(result)
        trade_text = live.auto_confirm.call_args.kwargs.get('trade_text')
        self.assertIn('/s/g1', trade_text)
        self.assertIn('/s/g2', trade_text)
        live._auto_delivery.assert_not_awaited()
        statuses = [c.kwargs.get('status') for c in live._persist_delivery_finalization_state.call_args_list]
        self.assertEqual(statuses.count('finalized'), 2)

    def test_order_already_delivery_escalates_manual(self):
        """P0灰度实测：平台对已发货订单返回 ORDER_ALREADY_DELIVERY（非 ORDER_STATUS_ERROR），
        必须走「升级人工」告警分支，而不是掉进笼统失败。"""
        live = self._gap_live(sent_units=[1, 2])
        # 模拟原始字符串直返（不带任何分类标志），验证字符串兜底判定
        live.auto_confirm = mock.AsyncMock(return_value={
            'error': 'ORDER_ALREADY_DELIVERY::已发货成功，请刷新页面~'})

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        alerted = live._send_ops_alert.call_args
        self.assertTrue(alerted.args[0].startswith('delivery_gap:'))
        self.assertIn('ORDER_ALREADY_DELIVERY', alerted.args[1])
        self.assertIn('人工', alerted.args[1])

    def test_flagged_already_delivered_escalates_without_string(self):
        live = self._gap_live(sent_units=[1, 2])
        live.auto_confirm = mock.AsyncMock(return_value={
            'error': '业务错误', 'order_already_delivered': True, 'order_status_error': True})

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        self.assertIn('人工', live._send_ops_alert.call_args.args[1])

    def test_manual_delivery_success_resolves_gap(self):
        """运营方已在控制台手动补发过的单子：直接结案，不再反复告警/补写。"""
        import db_manager as dbm
        live = self._gap_live(sent_units=[])
        live._get_pending_delivery_finalization_meta = lambda order_id, unit: None

        with mock.patch.object(dbm.db_manager, 'has_manual_delivery_success', return_value=True):
            result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))

        self.assertTrue(result)
        live.auto_confirm.assert_not_awaited()
        live._send_ops_alert.assert_not_awaited()
        statuses = [c.kwargs.get('status') for c in live._persist_delivery_finalization_state.call_args_list]
        self.assertEqual(statuses, ['finalized', 'finalized'])
        channels = [c.kwargs.get('channel') for c in live._persist_delivery_finalization_state.call_args_list]
        self.assertTrue(all(ch == 'manual_confirm' for ch in channels))

    def test_risk_log_dedup_and_resolve_on_success(self):
        """中-3：已有未结案缺口日志时不再重复写入；自愈后回写 success。"""
        live = self._gap_live(sent_units=[1, 2])
        with mock.patch.object(dbm.db_manager, 'get_latest_delivery_gap_risk_log',
                               return_value={'id': 55, 'processing_status': 'processing'}), \
                mock.patch.object(dbm.db_manager, 'add_risk_control_log',
                                  side_effect=AssertionError('不应重复写入')) as add_log, \
                mock.patch.object(dbm.db_manager, 'update_risk_control_log',
                                  return_value=True) as update_log:
            result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))

        self.assertTrue(result)
        add_log.assert_not_called()
        update_log.assert_any_call(55, processing_status='success', processing_result=mock.ANY)

    def test_no_recovery_gap_alert_uses_12h_cooldown(self):
        """无已送达内容=无自动恢复手段，末次告警冷却12小时防噪音。"""
        live = self._gap_live(sent_units=[])
        live._get_pending_delivery_finalization_meta = lambda order_id, unit: None

        asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertEqual(live._send_ops_alert.call_args.kwargs.get('cooldown_seconds'), 43200)

    def test_recoverable_gap_alert_keeps_2h_cooldown(self):
        live = self._gap_live(sent_units=[1, 2])
        live.auto_confirm = mock.AsyncMock(return_value={'error': 'SYSTEM_ERROR::系统繁忙，请稍后再试'})

        asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertEqual(live._send_ops_alert.call_args.kwargs.get('cooldown_seconds'), 7200)

    def test_auto_confirm_disabled_skips_rewrite(self):
        """低-1：账号关闭自动确认时，缺口处理器不得调用平台确认接口。"""
        live = self._gap_live(sent_units=[1, 2])
        live.is_auto_confirm_enabled = lambda: False

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        live.auto_confirm.assert_not_awaited()
        live._send_ops_alert.assert_awaited_once()

    def test_no_manual_record_still_alerts(self):
        import db_manager as dbm
        live = self._gap_live(sent_units=[])
        live._get_pending_delivery_finalization_meta = lambda order_id, unit: None

        with mock.patch.object(dbm.db_manager, 'has_manual_delivery_success', return_value=False):
            result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))

        self.assertFalse(result)
        live._send_ops_alert.assert_awaited_once()

    def test_order_status_error_escalates_manual(self):
        live = self._gap_live(sent_units=[1, 2])
        live.auto_confirm = mock.AsyncMock(return_value={
            'error': 'FAIL_BIZ_ORDER_STATUS_ERROR::ORDER_STATUS_ERROR', 'order_status_error': True,
        })

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        alerted = [c.args[0] for c in live._send_ops_alert.call_args_list]
        self.assertTrue(any(key.startswith('delivery_gap:') for key in alerted))
        self.assertIn('ORDER_STATUS_ERROR', live._send_ops_alert.call_args_list[0].args[1])

    def test_backoff_silence_no_network(self):
        live = self._gap_live(sent_units=[1, 2])
        live._get_manual_qr_wait_seconds = lambda: 600

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        live.auto_confirm.assert_not_awaited()
        live._send_ops_alert.assert_awaited_once()

    def test_no_sent_records_alert_manual_only(self):
        live = self._gap_live(sent_units=[])
        live._get_pending_delivery_finalization_meta = lambda order_id, unit: None

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        live.auto_confirm.assert_not_awaited()
        live._send_ops_alert.assert_awaited_once()
        self.assertIn('人工', live._send_ops_alert.call_args.args[1])

    def test_attempt_cooldown_prevents_repeat_consign(self):
        import time as _time
        live = self._gap_live(sent_units=[1, 2])
        live._delivery_gap_attempt_last['delivery_gap_attempt:poG'] = _time.time()

        result = asyncio.run(live._handle_delivery_gap_order(dict(self.ORDER), source='gap_test'))
        self.assertFalse(result)
        live.auto_confirm.assert_not_awaited()


class WatchdogGateTests(unittest.TestCase):
    def test_shipped_order_routes_to_gap_handler(self):
        import db_manager as dbm
        live = _direct_live('gate-unit')
        gap_handler = mock.AsyncMock(return_value=False)
        live._handle_delivery_gap_order = gap_handler
        order = {'order_id': 'poS', 'item_id': 'itS', 'buyer_id': 'bS', 'order_status': 'shipped'}

        with mock.patch.object(dbm.db_manager, '_normalize_order_status', return_value='shipped'):
            result = asyncio.run(live._auto_deliver_recovered_pending_order(order, source='gate_test'))

        self.assertFalse(result)
        gap_handler.assert_awaited_once()

    def test_other_status_still_skipped(self):
        import db_manager as dbm
        live = _direct_live('gate-unit')
        gap_handler = mock.AsyncMock()
        live._handle_delivery_gap_order = gap_handler
        order = {'order_id': 'poX', 'item_id': 'itX', 'buyer_id': 'bX', 'order_status': 'refunded'}

        with mock.patch.object(dbm.db_manager, '_normalize_order_status', return_value='refunded'):
            result = asyncio.run(live._auto_deliver_recovered_pending_order(order, source='gate_test'))

        self.assertFalse(result)
        gap_handler.assert_not_awaited()


class OpsAlertTests(unittest.TestCase):
    def test_alert_cooldown_on_success_and_failure(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'alert-unit'
        live._ops_alert_last_sent = {}

        with mock.patch.object(XianyuAutoAsync, 'dispatch_account_notifications',
                               new=mock.AsyncMock(return_value=False)) as dispatch:
            async def run():
                first = await live._send_ops_alert('k1', 'm1', cooldown_seconds=7200)
                second = await live._send_ops_alert('k1', 'm1', cooldown_seconds=7200)
                return first, second

            first, second = asyncio.run(run())

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(dispatch.await_count, 1)  # 失败也进冷却，第二次不再发送


class TokenNotifyCooldownTests(unittest.TestCase):
    def test_failed_notification_enters_cooldown(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'notify-unit'
        live.notification_lock = asyncio.Lock()
        live.last_notification_time = {}
        live.pending_notification_keys = set()
        live.message_stream_notification_cooldown = 1800
        live.token_refresh_notification_cooldown = 18000
        live.notification_cooldown = 300

        with mock.patch.object(XianyuAutoAsync, 'dispatch_account_notifications',
                               new=mock.AsyncMock(return_value=False)) as dispatch:
            async def run():
                await live.send_token_refresh_notification('RGV587_ERROR 风控硬拦截', 'token_refresh_failed')
                await live.send_token_refresh_notification('RGV587_ERROR 风控硬拦截', 'token_refresh_failed')

            asyncio.run(run())

        self.assertEqual(dispatch.await_count, 1)
        self.assertIn('token:token_refresh_failed', live.last_notification_time)


class GapScanTests(unittest.TestCase):
    def test_scan_routes_gap_orders(self):
        import db_manager as dbm
        live = _direct_live('scan-unit')
        gap_orders = [dict(GapHandlerTests.ORDER)]
        handler = mock.AsyncMock(return_value=True)
        live._handle_delivery_gap_order = handler

        with mock.patch.object(dbm.db_manager, 'get_shipped_orders_without_finalization',
                               return_value=gap_orders), \
                mock.patch.object(dbm.db_manager, 'get_pending_ship_orders', return_value=[]):
            handled = asyncio.run(live._scan_delivery_gap_orders())

        self.assertEqual(handled, 1)
        handler.assert_awaited_once()

    def test_scan_pending_stale_alert(self):
        live = _direct_live('scan-unit')
        stale_time = (datetime.now() - timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
        stale = [{'order_id': 'poP', 'item_id': 'itP', 'buyer_id': 'bP', 'buyer_nick': '买家P',
                  'sid': 's', 'order_status': 'pending_ship', 'updated_at': stale_time}]
        alert = mock.AsyncMock(return_value=True)
        live._send_ops_alert = alert
        live._get_stale_order_delivery_link = lambda order: ''

        with mock.patch.object(dbm.db_manager, 'get_shipped_orders_without_finalization',
                               return_value=[]), \
                mock.patch.object(dbm.db_manager, 'get_stale_pending_ship_orders', return_value=stale):
            asyncio.run(live._scan_delivery_gap_orders())

        alert.assert_awaited_once()
        self.assertTrue(alert.call_args.args[0].startswith('pending_ship_stale:poP'))


    def test_partial_prepared_units_skip_direct(self):
        """中-2 原子性守卫：多数量路径部分单元准备失败时，直填绝不整单 consign。"""
        live = _direct_live()
        live._get_order_expected_delivery_quantity = lambda order_id: 3

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1', 1),
                                _prepared_unit('https://pan.baidu.com/s/a3', 3)],
                source='test')

        self.assertIsNone(asyncio.run(run()))
        live.auto_confirm.assert_not_awaited()


class TimezoneTests(unittest.TestCase):
    def test_db_age_uses_utc_not_local(self):
        """高-1：库内时间戳为UTC，年龄计算必须用UTC，否则新订单被误判滞留8小时。"""
        utc_16min_ago = (datetime.now(timezone.utc) - timedelta(minutes=16)).strftime('%Y-%m-%d %H:%M:%S')
        age = XianyuLive._parse_db_datetime_age_seconds(utc_16min_ago)
        self.assertIsNotNone(age)
        self.assertTrue(900 <= age <= 1100, f'age={age}')
        # 本地时间字符串（比UTC快8小时）解析为负年龄 → 钳为0，不再虚增
        local_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.assertEqual(XianyuLive._parse_db_datetime_age_seconds(local_now), 0.0)
        self.assertIsNone(XianyuLive._parse_db_datetime_age_seconds(''))
        self.assertIsNone(XianyuLive._parse_db_datetime_age_seconds(None))


class GenerationTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._instances.pop('gen-unit', None)

    def test_patrol_generation_check(self):
        live = _direct_live('gen-unit')
        self.assertFalse(live._is_current_live_instance())  # 未注册（测试实例）
        XianyuLive._instances['gen-unit'] = live
        self.assertTrue(live._is_current_live_instance())
        XianyuLive._instances['gen-unit'] = object()  # 被热重载新实例顶替
        self.assertFalse(live._is_current_live_instance())


class PruneTests(unittest.TestCase):
    def test_prune_removes_expired_entries(self):
        live = _direct_live('prune-unit')
        live._ops_alert_last_sent = {'old': time.time() - 90000, 'fresh': time.time()}
        live._delivery_gap_attempt_last = {'old2': 1.0}
        live._prune_delivery_patrol_state()
        self.assertNotIn('old', live._ops_alert_last_sent)
        self.assertIn('fresh', live._ops_alert_last_sent)
        self.assertNotIn('old2', live._delivery_gap_attempt_last)


class StaleLinkTests(unittest.TestCase):
    def test_single_text_rule_provides_link(self):
        live = _direct_live('link-unit')
        with mock.patch.object(dbm.db_manager, 'get_item_info',
                               return_value={'item_title': '测试商品'}), \
                mock.patch.object(dbm.db_manager, 'get_delivery_rules_by_keyword',
                                  return_value=[{'card_type': 'text',
                                                 'text_content': 'https://pan.baidu.com/s/l1 提取码: ab12'}]):
            self.assertEqual(live._get_stale_order_delivery_link({'item_id': 'it1'}),
                             'https://pan.baidu.com/s/l1')

    def test_multiple_rules_or_non_text_yield_no_link(self):
        live = _direct_live('link-unit')
        with mock.patch.object(dbm.db_manager, 'get_item_info', return_value={'item_title': 'T'}), \
                mock.patch.object(dbm.db_manager, 'get_delivery_rules_by_keyword',
                                  return_value=[{'card_type': 'text', 'text_content': 'x'},
                                                {'card_type': 'text', 'text_content': 'y'}]):
            self.assertEqual(live._get_stale_order_delivery_link({'item_id': 'it1'}), '')
        with mock.patch.object(dbm.db_manager, 'get_item_info', return_value={'item_title': 'T'}), \
                mock.patch.object(dbm.db_manager, 'get_delivery_rules_by_keyword',
                                  return_value=[{'card_type': 'data', 'card_id': 9}]):
            self.assertEqual(live._get_stale_order_delivery_link({'item_id': 'it1'}), '')


if __name__ == '__main__':
    unittest.main()
