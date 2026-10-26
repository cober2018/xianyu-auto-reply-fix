"""双通道履约改造（发货凭证直填 + 私信通知）单元测试。

验收映射：
- A1 直填+私信双成功 / A2 直填成功+私信失败（记账+告警，不再静默）/
  A3 无私聊通道（缺口持久化+告警）/ A4 直填失败（不落私信账、回落不变）
- A5 巡检自动补发成功结案 / A6 控制台人工补发与人工标记结案
- B1 巡检能扫到「凭证已送达+私信未送达」/ B2 长连接在线自动补发 /
  B3 长连接不在线不盲目补发 / B4 无直填证据不误报 / B5 失败退避+次数上限+告警冷却
- C1 存量老订单不误报（真库 SQL）/ C2 双通道同单元共存互不覆盖（真库 SQL）
- 幂等红线：已送达通道行永不被降级；掉线恢复补拉调度
"""
import asyncio
import json
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import db_manager as dbm
import XianyuAutoAsync
from XianyuAutoAsync import XianyuLive


SNAPSHOT_STEPS = [{'type': 'text', 'content': 'https://pan.baidu.com/s/x1 提取码: ab12'}]


def _direct_live(cookie_id='dual-unit'):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = cookie_id
    live.confirmed_orders = {}
    live.order_confirm_cooldown = 600
    live._ops_alert_last_sent = {}
    live._delivery_gap_attempt_last = {}
    live._last_orders_recovery_sync_time = 0.0
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


def _prepared_unit(content, unit_index=1):
    return {
        'unit_index': unit_index,
        'delivery_steps': [{'type': 'text', 'content': content}],
        'rule_meta': {'success': True, 'rule_id': 7, 'card_type': 'text',
                      'data_line': content, 'delivery_unit_index': unit_index},
    }


def _gap_live(cookie_id='dual-gap'):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = cookie_id
    live._ops_alert_last_sent = {}
    live._delivery_gap_attempt_last = {}
    live._last_orders_recovery_sync_time = 0.0
    live._chat_gap_resend_inflight = set()
    live.user_id = 1
    live.ws = None
    live._record_delivery_log = mock.MagicMock()
    live._send_ops_alert = mock.AsyncMock(return_value=True)
    live._get_manual_qr_wait_seconds = lambda: 0
    return live


def _chat_row(unit_index=1, status='pending', attempt_count=0, next_retry_at='2000-01-01 00:00:00',
              snapshot=None):
    return {
        'order_id': 'poG', 'unit_index': unit_index, 'channel': 'chat', 'status': status,
        'content_snapshot': snapshot if snapshot is not None else json.dumps(SNAPSHOT_STEPS),
        'attempt_count': attempt_count, 'last_error': '无私聊通道', 'next_retry_at': next_retry_at,
    }


class ChatGapHandlerTests(unittest.TestCase):
    """私信缺口巡检处理：自动补发状态机（B1-B5 / A5 / 2.7 幂等）。"""

    ORDER = {'order_id': 'poG', 'item_id': 'itG', 'buyer_id': 'bG', 'buyer_nick': '买家G',
             'sid': 'cid@goofish', 'order_status': 'shipped',
             'created_at': '2026-10-26 02:00:00', 'updated_at': '2026-10-26 02:31:00'}

    def setUp(self):
        self.patches = {}
        self.db_state = {
            'channel_states': [_chat_row()],
            'finalization_states': [],
            'manual_success': False,
            'upserts': [],
            'gap_log': None,
        }

        def _upsert(order_id, unit_index, channel, status, **kwargs):
            self.db_state['upserts'].append(
                {'order_id': order_id, 'unit_index': unit_index, 'channel': channel,
                 'status': status, **kwargs})
            return True

        def _get_states(order_id):
            return list(self.db_state['channel_states'])

        def _get_finalizations(order_id):
            return list(self.db_state['finalization_states'])

        for target in (
            mock.patch.object(dbm.db_manager, 'upsert_delivery_channel_state', side_effect=_upsert),
            mock.patch.object(dbm.db_manager, 'get_delivery_channel_states', side_effect=_get_states),
            mock.patch.object(dbm.db_manager, 'get_delivery_finalization_states', side_effect=_get_finalizations),
            mock.patch.object(dbm.db_manager, 'has_manual_delivery_success',
                              side_effect=lambda oid: self.db_state['manual_success']),
            mock.patch.object(dbm.db_manager, 'add_risk_control_log', return_value=77),
            mock.patch.object(dbm.db_manager, 'get_latest_delivery_gap_risk_log',
                              side_effect=lambda *a, **k: self.db_state['gap_log']),
            mock.patch.object(dbm.db_manager, 'update_risk_control_log', return_value=True),
        ):
            self.patches[target.attribute] = target
            target.start()
            self.addCleanup(target.stop)

    def _states_after(self):
        return self.db_state['upserts']

    def test_resend_success_marks_delivered_and_resolves(self):
        """A5/B2：长连接在线 → 原样快照补发成功 → 通道行翻转为 delivered + 风险日志结案。"""
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock(return_value=None)

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertTrue(result)
        live._send_delivery_steps.assert_awaited_once()
        args, kwargs = live._send_delivery_steps.call_args
        self.assertEqual(args[1], 'cid')  # sid 去掉 @goofish
        delivered = [u for u in self._states_after() if u['status'] == 'delivered']
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['unit_index'], 1)
        self.assertEqual(delivered[0]['last_error'], '')
        self.assertEqual(dbm.db_manager.update_risk_control_log.call_count, 1)
        self.assertIn('✅', live._send_ops_alert.call_args.args[1])

    def test_offline_does_not_send_and_alerts_reason(self):
        """B3：长连接不在线 → 不补发、告警说明原因、通道行保持未送达。"""
        live = _gap_live()  # ws = None

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        delivered = [u for u in self._states_after() if u['status'] in ('delivered', 'manual_delivered')]
        self.assertEqual(delivered, [])
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('长连接未就绪', alert_text)

    def test_send_failure_bumps_attempt_and_schedules_backoff(self):
        """B5：补发失败 → 计次+退避调度+告警含重试计划。"""
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock(side_effect=RuntimeError('回执超时'))

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        failed_update = [u for u in self._states_after() if u.get('bump_attempt')]
        self.assertEqual(len(failed_update), 1)
        self.assertEqual(failed_update[0]['status'], 'pending')
        self.assertTrue(failed_update[0]['next_retry_at'])
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('已自动重试 1/3', alert_text)
        self.assertIn('回执超时', alert_text)
        # BUG-4：发送结束后 in-flight 守卫必须释放，不阻塞后续重试
        self.assertEqual(live._chat_gap_resend_inflight, set())

    def test_max_attempts_stops_retry_and_requires_manual(self):
        """B5：达到次数上限 → 不再发送，转人工告警。"""
        self.db_state['channel_states'] = [_chat_row(status='failed', attempt_count=3)]
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('已达最大自动补发次数', alert_text)
        self.assertIn('需人工', alert_text)

    def test_grace_period_waits_and_warns_first_discovery(self):
        """宽限期内：只告警不发送（给运营方标记结案的反应窗口，2.7 铁律缓冲）。"""
        from datetime import timedelta
        future = (datetime.now(timezone.utc) + timedelta(seconds=600)).strftime('%Y-%m-%d %H:%M:%S')
        self.db_state['channel_states'] = [_chat_row(next_retry_at=future)]
        live = _gap_live()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('宽限期', alert_text)
        self.assertIn('manual-mark', alert_text)

    def test_manual_delivery_record_closes_gap_without_sending(self):
        """控制台人工补发记录 → 通道行标记 manual_delivered，绝不重发。"""
        self.db_state['manual_success'] = True
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertTrue(result)
        live._send_delivery_steps.assert_not_awaited()
        manual = [u for u in self._states_after() if u['status'] == 'manual_delivered']
        self.assertEqual(len(manual), 1)

    def test_manual_close_resolves_risk_log_and_backfills_units(self):
        """BUG-1/2 修复：人工结案回写已建风险日志；无 chat 行的存量单按 trade_text 单元回填结案。"""
        self.db_state['manual_success'] = True
        self.db_state['gap_log'] = {'id': 55, 'processing_status': 'processing'}
        self.db_state['channel_states'] = []  # 部署前直填的存量单：没有任何 chat 行
        self.db_state['finalization_states'] = [
            {'order_id': 'poG', 'unit_index': 1, 'channel': 'trade_text', 'status': 'finalized',
             'delivery_meta': {'data_line': 'https://pan.baidu.com/s/u1 提取码: aa11'}},
            {'order_id': 'poG', 'unit_index': 2, 'channel': 'trade_text', 'status': 'finalized',
             'delivery_meta': {'data_line': 'https://pan.baidu.com/s/u2 提取码: bb22'}},
        ]
        live = _gap_live()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertTrue(result)
        live._send_delivery_steps.assert_not_awaited()
        manual = [u for u in self._states_after() if u['status'] == 'manual_delivered']
        self.assertEqual(sorted(u['unit_index'] for u in manual), [1, 2])
        # BUG-1：已建缺口风险日志必须结案回写，否则风控面板残留 processing
        self.assertEqual(dbm.db_manager.update_risk_control_log.call_args.args[0], 55)
        self.assertEqual(dbm.db_manager.update_risk_control_log.call_args.kwargs.get('processing_status'), 'success')

    def test_missing_sid_requires_manual_without_burning_attempt(self):
        """BUG-6 修复：巡检自动补发要求 sid；缺 sid 转人工，不烧补发计次。"""
        order = dict(self.ORDER)
        order['sid'] = ''
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(order))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        self.assertIn('会话ID', live._send_ops_alert.call_args.args[1])
        self.assertEqual([u for u in self._states_after() if u.get('bump_attempt')], [])

    def test_concurrent_resend_guard_skips_duplicate_send(self):
        """BUG-4 修复：in-flight 期间重复触发（巡检与控制台并发）只允许一路发送。"""
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()
        live._chat_gap_resend_inflight = {'poG'}  # 另一路补发正在进行

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        self.assertEqual(self._states_after(), [])

    def test_login_backoff_silence_no_network(self):
        """退避静默期：不发任何网络请求，只告警。"""
        live = _gap_live()
        live._get_manual_qr_wait_seconds = lambda: 300
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        self.assertIn('退避', live._send_ops_alert.call_args.args[1])

    def test_auto_resend_disabled_alerts_only(self):
        """总开关关闭：只记账告警，不补发（回滚开关行为）。"""
        live = _gap_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock()

        with mock.patch.dict(XianyuAutoAsync.DELIVERY, {'chat_gap_auto_resend_enabled': False}):
            result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        live._send_delivery_steps.assert_not_awaited()
        self.assertIn('已关闭', live._send_ops_alert.call_args.args[1])

    def test_backfills_missing_chat_rows_from_trade_text_finalization(self):
        """部署前直填订单（无 chat 行）：从 trade_text 完成记录回填快照后按同一套调度处理。"""
        self.db_state['channel_states'] = []
        self.db_state['finalization_states'] = [
            {'order_id': 'poG', 'unit_index': 1, 'channel': 'trade_text', 'status': 'finalized',
             'delivery_meta': {'data_line': 'https://pan.baidu.com/s/y9 提取码: zz99'}}]
        live = _gap_live()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)  # 回填后进入宽限期，不发送
        backfill = [u for u in self._states_after() if u['channel'] == 'chat' and u['status'] == 'pending']
        self.assertEqual(len(backfill), 1)
        self.assertIn('zz99', backfill[0]['content_snapshot'])
        live._send_delivery_steps.assert_not_awaited()

    def test_no_trade_text_evidence_is_ignored(self):
        """B4：没有直填送达证据（旧单通道订单）→ 不处理、不误报。"""
        self.db_state['channel_states'] = []
        self.db_state['finalization_states'] = [
            {'order_id': 'poG', 'unit_index': 1, 'channel': 'auto', 'status': 'finalized',
             'delivery_meta': {}}]
        live = _gap_live()
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(live._handle_chat_notification_gap_order(dict(self.ORDER)))

        self.assertFalse(result)
        self.assertEqual(self._states_after(), [])
        live._send_ops_alert.assert_not_awaited()


class DirectChannelBookkeepingTests(unittest.TestCase):
    """直填成功后的私信通道记账 + 即时告警（A1-A4）。"""

    def test_a1_both_channels_success_no_alert(self):
        """A1：直填+私信双成功 → 通道行 pending→delivered，无告警。"""
        live = _direct_live()
        live._send_delivery_steps = mock.AsyncMock(return_value=None)

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=object(), chat_id='cid1', source='test')

        self.assertTrue(asyncio.run(run()))
        statuses = [c.kwargs.get('status') for c in live._record_chat_channel_state.call_args_list]
        self.assertEqual(statuses, ['pending', 'delivered'])
        live._send_ops_alert.assert_not_awaited()
        success_logs = [c for c in live._record_delivery_log.call_args_list
                        if c.kwargs.get('channel') == 'chat' and c.kwargs.get('status') == 'success']
        self.assertEqual(len(success_logs), 1)

    def test_a2_chat_failure_persists_gap_and_alerts(self):
        """A2：私信发送失败 → 通道保持 pending（含原因）+ chat 失败日志 + 即时告警。"""
        live = _direct_live()
        live._send_delivery_steps = mock.AsyncMock(side_effect=RuntimeError('回执超时'))

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=object(), chat_id='cid1', source='test')

        self.assertTrue(asyncio.run(run()))  # 履约不受影响
        failed_calls = [c for c in live._record_chat_channel_state.call_args_list
                        if c.kwargs.get('status') == 'pending' and c.kwargs.get('last_error')]
        self.assertEqual(len(failed_calls), 1)
        self.assertIn('回执超时', failed_calls[0].kwargs['last_error'])
        chat_failed_logs = [c for c in live._record_delivery_log.call_args_list
                            if c.kwargs.get('channel') == 'chat' and c.kwargs.get('status') == 'failed']
        self.assertEqual(len(chat_failed_logs), 1)
        live._send_ops_alert.assert_awaited_once()
        self.assertEqual(live._send_ops_alert.call_args.args[0], 'chat_gap:po1')

    def test_a3_no_chat_channel_persists_gap_and_alerts(self):
        """A3：无私聊通道（如扫码稳定期）→ 缺口持久化 + 告警，不静默。"""
        live = _direct_live()

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=None, source='test')

        self.assertTrue(asyncio.run(run()))
        pending_calls = live._record_chat_channel_state.call_args_list
        self.assertTrue(all(c.kwargs.get('status') == 'pending' for c in pending_calls))
        live._send_ops_alert.assert_awaited_once()
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('私信通知缺口', alert_text)
        self.assertIn('宽限期', alert_text)

    def test_a4_consign_failure_leaves_no_chat_bookkeeping(self):
        """A4：直填失败 → 不落任何私信账，回落逻辑不变。"""
        live = _direct_live()
        live.auto_confirm = mock.AsyncMock(return_value={'error': 'FAIL_SYS_SESSION_EXPIRED::Session过期'})

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=object(), chat_id='cid1', source='test')

        self.assertFalse(asyncio.run(run()))
        live._record_chat_channel_state.assert_not_called()
        live._send_ops_alert.assert_not_awaited()

    def test_a2_alert_contains_order_time_and_age(self):
        """遗漏-3 修复：直填即时告警也带下单时间与滞留时长（2.6 全要素）。"""
        live = _direct_live()
        live._send_delivery_steps = mock.AsyncMock(side_effect=RuntimeError('回执超时'))
        recent_utc = datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')

        async def run():
            return await live._deliver_via_trade_text_direct(
                order_id='po1', item_id='it1', buyer_id='b1',
                prepared_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
                websocket=object(), chat_id='cid1', source='test')

        with mock.patch.object(dbm.db_manager, 'get_order_by_id',
                               return_value={'order_id': 'po1', 'created_at': recent_utc}):
            self.assertTrue(asyncio.run(run()))
        alert_text = live._send_ops_alert.call_args.args[1]
        self.assertIn('下单时间', alert_text)
        self.assertIn('滞留', alert_text)
        self.assertTrue('分钟' in alert_text or '小时' in alert_text)

    def test_chat_gap_alert_message_contains_all_required_fields(self):
        """2.6：告警正文含订单/买家/滞留/通道/可见内容/重试计划/人工指引。"""
        live = _direct_live()
        message = live._build_chat_gap_alert_message(
            order_id='po9', item_id='it9', buyer_id='b9', buyer_nick='买家九',
            created_at_text='2026-10-26 02:00:00', age_text='3.5 小时',
            chat_reason='无私聊通道（长连接未就绪）',
            sent_hint_units=[_prepared_unit('https://pan.baidu.com/s/a1 提取码: 8k2p')],
            plan_text='系统将在宽限期约 15 分钟后自动补发私信')

        self.assertIn('po9', message)
        self.assertIn('买家九', message)
        self.assertIn('滞留 3.5 小时', message)
        self.assertIn('发货凭证: ✅', message)
        self.assertIn('https://pan.baidu.com/s/a1', message)
        self.assertIn('提取码: 8k2p', message)
        self.assertIn('私信通知: ❌', message)
        self.assertIn('自动补发:', message)
        self.assertIn('manual-mark', message)


class ScanWiringTests(unittest.TestCase):
    """巡检接线：私信缺口扫描命中即交处理器。"""

    def test_scan_routes_chat_gap_orders(self):
        live = _gap_live()
        live._send_delivery_steps = mock.AsyncMock()
        order = {'order_id': 'poS', 'item_id': 'itS', 'buyer_id': 'bS', 'buyer_nick': '买家S',
                 'sid': '', 'order_status': 'shipped', 'created_at': '2026-10-26 02:00:00',
                 'updated_at': '2026-10-26 02:31:00'}

        with mock.patch.object(dbm.db_manager, 'get_shipped_orders_without_finalization', return_value=[]), \
                mock.patch.object(dbm.db_manager, 'get_chat_notification_gap_orders',
                                  return_value=[order]) as gap_query, \
                mock.patch.object(dbm.db_manager, 'get_stale_pending_ship_orders', return_value=[]), \
                mock.patch.object(XianyuLive, '_handle_chat_notification_gap_order',
                                  new=mock.AsyncMock(return_value=True)) as handler:
            handled = asyncio.run(live._scan_delivery_gap_orders())

        self.assertEqual(handled, 1)
        gap_query.assert_called_once()
        handler.assert_awaited_once()
        self.assertEqual(handler.call_args.kwargs.get('source'), 'chat_gap_patrol')


class ChannelStoreSqlTests(unittest.TestCase):
    """真库 SQL：C1 存量不误报 / C2 双通道共存 / upsert 幂等红线 / 缺口扫描判据。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='dual_channel_test_')
        cls.store = dbm.DBManager(db_path=f'{cls.tmpdir}/dual.db')

    @classmethod
    def tearDownClass(cls):
        try:
            cls.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _utc_now_str(self):
        return datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y-%m-%d %H:%M:%S')

    def _seed_order(self, order_id, created_at=None):
        with self.store.lock:
            cursor = self.store.conn.cursor()
            cursor.execute(
                '''
                INSERT INTO orders (order_id, item_id, buyer_id, buyer_nick, sid, order_status,
                                    cookie_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET updated_at = excluded.updated_at
                ''',
                (order_id, 'it', 'b', '买家', f'sid-{order_id}@goofish', 'shipped',
                 'cookie-x', created_at or self._utc_now_str(), self._utc_now_str()))
            self.store.conn.commit()

    def test_c2_two_channels_coexist_same_unit(self):
        """C2：同一订单同一单元，trade_text 与 chat 两行共存、互不覆盖。"""
        self.store.upsert_delivery_channel_state('poC2', 1, 'trade_text', 'delivered',
                                                 cookie_id='cookie-x')
        self.store.upsert_delivery_channel_state('poC2', 1, 'chat', 'pending', cookie_id='cookie-x')
        rows = self.store.get_delivery_channel_states('poC2')
        by_channel = {r['channel']: r for r in rows}
        self.assertEqual(by_channel['trade_text']['status'], 'delivered')
        self.assertEqual(by_channel['chat']['status'], 'pending')
        # 直填完成记录照旧落 finalize 表，互不干扰
        self.store.upsert_delivery_finalization_state('poC2', 1, 'cookie-x', 'it', 'b',
                                                      channel='trade_text', status='finalized')
        self.assertEqual(len(self.store.get_delivery_finalization_states('poC2')), 1)

    def test_delivered_row_never_downgraded(self):
        """幂等红线：已送达通道行不允许被后续写入降级回 pending。"""
        self.store.upsert_delivery_channel_state('poIdem', 1, 'chat', 'delivered',
                                                 content_snapshot='[]')
        self.store.upsert_delivery_channel_state('poIdem', 1, 'chat', 'pending',
                                                 last_error='迟到的失败写入')
        row = [r for r in self.store.get_delivery_channel_states('poIdem')][0]
        self.assertEqual(row['status'], 'delivered')
        self.assertIsNotNone(row['delivered_at'])

    def test_attempt_count_bumps_and_delivered_at_set(self):
        self.store.upsert_delivery_channel_state('poBump', 1, 'chat', 'pending')
        self.store.upsert_delivery_channel_state('poBump', 1, 'chat', 'failed', bump_attempt=True)
        row = self.store.get_delivery_channel_states('poBump')[0]
        self.assertEqual(row['attempt_count'], 1)
        self.store.upsert_delivery_channel_state('poBump', 1, 'chat', 'delivered')
        row = self.store.get_delivery_channel_states('poBump')[0]
        self.assertEqual(row['attempt_count'], 1)  # 送达不计次
        self.assertIsNotNone(row['delivered_at'])

    def test_c1_legacy_orders_not_flagged_as_chat_gap(self):
        """C1：旧单通道订单（无 trade_text 证据）绝不被私信缺口扫描命中。"""
        self._seed_order('poLegacy', created_at=self._utc_now_str())
        self.store.upsert_delivery_finalization_state('poLegacy', 1, 'cookie-x', 'it', 'b',
                                                      channel='auto', status='finalized')
        matched = {r['order_id'] for r in self.store.get_chat_notification_gap_orders('cookie-x', 72, 10)}
        self.assertNotIn('poLegacy', matched)

    def test_gap_scan_matches_trade_text_without_chat_delivery(self):
        """B1：凭证已送达 + 私信未送达 → 命中；私信已送达/人工结案 → 不命中。"""
        self._seed_order('poGap1')
        self._seed_order('poGap2')
        self._seed_order('poGap3')
        for order_id in ('poGap1', 'poGap2', 'poGap3'):
            self.store.upsert_delivery_finalization_state(order_id, 1, 'cookie-x', 'it', 'b',
                                                          channel='trade_text', status='finalized')
        self.store.upsert_delivery_channel_state('poGap1', 1, 'chat', 'pending')
        self.store.upsert_delivery_channel_state('poGap2', 1, 'chat', 'delivered')
        self.store.upsert_delivery_channel_state('poGap3', 1, 'chat', 'manual_delivered')

        matched = {r['order_id'] for r in self.store.get_chat_notification_gap_orders('cookie-x', 72, 10)}
        self.assertIn('poGap1', matched)
        self.assertNotIn('poGap2', matched)
        self.assertNotIn('poGap3', matched)

    def test_gap_scan_respects_time_window(self):
        """时间窗外的老订单不再骚扰（72 小时窗口）。"""
        old_time = '2026-01-01 00:00:00'
        self._seed_order('poOld', created_at=old_time)
        self.store.upsert_delivery_finalization_state('poOld', 1, 'cookie-x', 'it', 'b',
                                                      channel='trade_text', status='finalized')
        self.store.upsert_delivery_channel_state('poOld', 1, 'chat', 'pending')
        matched = {r['order_id'] for r in self.store.get_chat_notification_gap_orders('cookie-x', 72, 10)}
        self.assertNotIn('poOld', matched)

    def test_partial_unit_gap_still_matched(self):
        """BUG-3 修复：多单元订单部分单元私信未送达仍命中，不被「任一单元已送达」整体排除。"""
        self._seed_order('poPart')
        self._seed_order('poAll')
        for order_id in ('poPart', 'poAll'):
            for unit in (1, 2):
                self.store.upsert_delivery_finalization_state(order_id, unit, 'cookie-x', 'it', 'b',
                                                              channel='trade_text', status='finalized')
        # poPart：单元1已送达、单元2未送达 → 必须命中；poAll：两单元都送达 → 不命中
        self.store.upsert_delivery_channel_state('poPart', 1, 'chat', 'delivered')
        self.store.upsert_delivery_channel_state('poPart', 2, 'chat', 'pending')
        self.store.upsert_delivery_channel_state('poAll', 1, 'chat', 'delivered')
        self.store.upsert_delivery_channel_state('poAll', 2, 'chat', 'delivered')

        matched = {r['order_id'] for r in self.store.get_chat_notification_gap_orders('cookie-x', 72, 10)}
        self.assertIn('poPart', matched)
        self.assertNotIn('poAll', matched)

    def test_bulk_view_queries_group_by_order(self):
        """C3 批量查询：按账号一次取回，按订单分组。"""
        self._seed_order('poBulk1')
        self._seed_order('poBulk2')
        self.store.upsert_delivery_finalization_state('poBulk1', 1, 'cookie-x', 'it', 'b',
                                                      channel='trade_text', status='finalized')
        self.store.upsert_delivery_finalization_state('poBulk1', 2, 'cookie-x', 'it', 'b',
                                                      channel='trade_text', status='sent')
        self.store.upsert_delivery_channel_state('poBulk1', 1, 'chat', 'delivered', cookie_id='cookie-x')
        trade_map = self.store.get_trade_text_delivery_units_by_cookie(['cookie-x'])
        self.assertEqual(sorted(trade_map.get('poBulk1')), [1, 2])
        chat_map = self.store.get_chat_channel_states_by_cookie(['cookie-x'])
        self.assertEqual(chat_map.get('poBulk1'), [{'unit_index': 1, 'status': 'delivered'}])


class RecoverySyncTests(unittest.TestCase):
    """2.8：掉线恢复在线后的一次性订单补拉调度。"""

    def _sync_live(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'sync-live'
        live.user_id = 1
        live._last_orders_recovery_sync_time = 0.0
        live.connection_state = XianyuAutoAsync.ConnectionState.CONNECTED
        live._is_current_live_instance = lambda: True
        live._get_manual_qr_wait_seconds = lambda: 0
        live._interruptible_sleep = mock.AsyncMock()
        live._recover_recent_missing_orders = mock.AsyncMock(return_value=3)
        captured = []
        live._create_tracked_task = captured.append
        return live, captured

    def test_schedules_once_and_runs_recovery(self):
        live, captured = self._sync_live()
        live._maybe_schedule_orders_recovery_sync(reason='test')
        self.assertEqual(len(captured), 1)
        asyncio.run(captured[0])
        live._recover_recent_missing_orders.assert_awaited_once_with(max_orders=20)
        self.assertGreater(live._last_orders_recovery_sync_time, 0)

    def test_min_interval_prevents_reconnect_storm(self):
        live, captured = self._sync_live()
        live._last_orders_recovery_sync_time = time.time()
        live._maybe_schedule_orders_recovery_sync(reason='test')
        self.assertEqual(captured, [])

    def test_disabled_by_config(self):
        live, captured = self._sync_live()
        with mock.patch.dict(XianyuAutoAsync.DELIVERY, {'orders_recovery_sync_enabled': False}):
            live._maybe_schedule_orders_recovery_sync(reason='test')
        self.assertEqual(captured, [])

    def test_skips_when_connection_not_ready(self):
        live, captured = self._sync_live()
        live.connection_state = XianyuAutoAsync.ConnectionState.RECONNECTING
        live._maybe_schedule_orders_recovery_sync(reason='test')
        asyncio.run(captured[0])
        live._recover_recent_missing_orders.assert_not_awaited()


class ReplyServerChatResendTests(unittest.TestCase):
    """A6：控制台手动发货入口的私信原样补发 + 人工标记结案。"""

    ORDER = {'order_id': 'poR', 'item_id': 'itR', 'buyer_id': 'bR', 'buyer_nick': '买家R',
             'sid': 'cid@goofish', 'cookie_id': 'cookie-x'}

    def setUp(self):
        import reply_server
        self.rs = reply_server
        self.upserts = []
        self.db_state = {'states': [_chat_row()], 'gap_log': {'id': 9, 'processing_status': 'processing'}}

        def _upsert(order_id, unit_index, channel, status, **kwargs):
            self.upserts.append({'order_id': order_id, 'unit_index': unit_index,
                                 'channel': channel, 'status': status, **kwargs})
            return True

        for target in (
            mock.patch.object(dbm.db_manager, 'get_delivery_channel_states',
                              side_effect=lambda oid: list(self.db_state['states'])),
            mock.patch.object(dbm.db_manager, 'upsert_delivery_channel_state', side_effect=_upsert),
            mock.patch.object(dbm.db_manager, 'create_delivery_log', return_value=True),
            mock.patch.object(dbm.db_manager, 'get_latest_delivery_gap_risk_log',
                              side_effect=lambda *a, **k: self.db_state['gap_log']),
            mock.patch.object(dbm.db_manager, 'update_risk_control_log', return_value=True),
            mock.patch.object(self.rs, 'publish_order_update_event', return_value=None),
        ):
            target.start()
            self.addCleanup(target.stop)

    def _resend_live(self):
        live = _gap_live()
        live._backfill_chat_states_from_finalization = mock.MagicMock(return_value=0)
        return live

    def test_resends_snapshot_and_marks_delivered(self):
        live = self._resend_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock(return_value=None)
        user = {'user_id': 1, 'username': 'ops'}

        result = asyncio.run(self.rs._resend_order_chat_notification_snapshot(
            live, dict(self.ORDER), 'cookie-x', user))

        self.assertTrue(result['success'])
        self.assertEqual(result.get('channel'), 'chat')
        live._send_delivery_steps.assert_awaited_once()
        self.assertEqual(live._send_delivery_steps.call_args.args[1], 'cid')
        delivered = [u for u in self.upserts if u['status'] == 'delivered']
        self.assertEqual(len(delivered), 1)

    def test_no_gap_returns_none(self):
        self.db_state['states'] = [_chat_row(status='delivered')]
        live = self._resend_live()
        result = asyncio.run(self.rs._resend_order_chat_notification_snapshot(
            live, dict(self.ORDER), 'cookie-x', {'user_id': 1, 'username': 'ops'}))
        self.assertIsNone(result)

    def test_offline_returns_guidance_not_failure(self):
        live = self._resend_live()  # ws None
        result = asyncio.run(self.rs._resend_order_chat_notification_snapshot(
            live, dict(self.ORDER), 'cookie-x', {'user_id': 1, 'username': 'ops'}))
        self.assertFalse(result['success'])
        self.assertIn('manual-mark', result['message'])
        self.assertEqual([u for u in self.upserts if u['status'] == 'delivered'], [])

    def test_send_failure_records_pending_with_reason(self):
        live = self._resend_live()
        live.ws = object()
        live._send_delivery_steps = mock.AsyncMock(side_effect=RuntimeError('ack 超时'))
        result = asyncio.run(self.rs._resend_order_chat_notification_snapshot(
            live, dict(self.ORDER), 'cookie-x', {'user_id': 1, 'username': 'ops'}))
        self.assertFalse(result['success'])
        bumped = [u for u in self.upserts if u.get('bump_attempt')]
        self.assertEqual(len(bumped), 1)
        self.assertIn('ack 超时', bumped[0]['last_error'])

    def test_resend_skips_when_auto_resend_inflight(self):
        """BUG-4 修复（控制台侧）：巡检自动补发进行中时，手动补发入口拒绝重复发送。"""
        live = self._resend_live()
        live.ws = object()
        live._chat_gap_resend_inflight = {'poR'}
        live._send_delivery_steps = mock.AsyncMock()

        result = asyncio.run(self.rs._resend_order_chat_notification_snapshot(
            live, dict(self.ORDER), 'cookie-x', {'user_id': 1, 'username': 'ops'}))

        self.assertFalse(result['success'])
        self.assertIn('自动补发中', result['message'])
        live._send_delivery_steps.assert_not_awaited()
        self.assertEqual([u for u in self.upserts if u['status'] == 'delivered'], [])

    def test_manual_mark_endpoint_closes_gap_without_sending(self):
        import asyncio as _asyncio

        with mock.patch.object(dbm.db_manager, 'get_order_by_id', return_value=dict(self.ORDER)), \
                mock.patch.object(dbm.db_manager, 'get_cookie_details',
                                  return_value={'user_id': 1}), \
                mock.patch('cookie_manager.manager', None):
            result = _asyncio.run(
                self.rs.mark_chat_notification_manually_delivered('poR', {'user_id': 1, 'username': 'ops'}))

        self.assertTrue(result['success'])
        manual = [u for u in self.upserts if u['status'] == 'manual_delivered']
        self.assertEqual(len(manual), 1)
        self.assertEqual(self.rs.db_manager.update_risk_control_log.call_count, 1)


class FulfillmentViewTests(unittest.TestCase):
    """C3：订单列表的双通道履约完整度派生视图。"""

    def test_complete_and_incomplete_orders(self):
        import reply_server
        orders = [
            {'order_id': 'poF1'},  # 双通道齐（含人工结案）→ complete
            {'order_id': 'poF2'},  # 单元2私信未送达 → 不完整
            {'order_id': 'poF3'},  # 无直填证据 → 不携带字段（旧单通道订单）
        ]
        trade_map = {'poF1': [1, 2], 'poF2': [1, 2]}
        chat_map = {
            'poF1': [{'unit_index': 1, 'status': 'delivered'},
                     {'unit_index': 2, 'status': 'manual_delivered'}],
            'poF2': [{'unit_index': 1, 'status': 'delivered'},
                     {'unit_index': 2, 'status': 'pending'}],
        }
        with mock.patch.object(dbm.db_manager, 'get_trade_text_delivery_units_by_cookie',
                               return_value=trade_map), \
                mock.patch.object(dbm.db_manager, 'get_chat_channel_states_by_cookie',
                                  return_value=chat_map):
            reply_server._attach_order_fulfillment_views(orders, ['cookie-x'])

        self.assertTrue(orders[0]['fulfillment']['complete'])
        self.assertEqual(orders[0]['fulfillment']['chat']['delivered_units'], [1, 2])
        self.assertEqual(orders[0]['fulfillment']['chat']['pending_units'], [])
        self.assertFalse(orders[1]['fulfillment']['complete'])
        self.assertEqual(orders[1]['fulfillment']['chat']['pending_units'], [2])
        self.assertNotIn('fulfillment', orders[2])

    def test_db_error_leaves_orders_untouched(self):
        import reply_server
        orders = [{'order_id': 'poF9'}]
        with mock.patch.object(dbm.db_manager, 'get_trade_text_delivery_units_by_cookie',
                               side_effect=RuntimeError('db down')):
            reply_server._attach_order_fulfillment_views(orders, ['cookie-x'])
        self.assertNotIn('fulfillment', orders[0])


if __name__ == '__main__':
    unittest.main()
