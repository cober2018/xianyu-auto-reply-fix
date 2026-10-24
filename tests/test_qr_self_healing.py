import asyncio
import time
import unittest
from unittest import mock

from XianyuAutoAsync import ConnectionState, XianyuLive


def _live():
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "qr-unit"
    live.last_token_refresh_status = "hard_block"
    live.last_token_refresh_error_message = "命中硬拦截"
    live.last_password_login_backoff_log_time = 123.0
    live.connection_failures = 3
    live.init_auth_failures = 2
    live.last_slider_success_at = 0.0
    live._fulfillment_priority_depth = 0
    live._login_backoff_wake_event = asyncio.Event()
    return live


class ClearBackoffTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive.clear_password_login_failure_backoff("qr-unit")
        XianyuLive.clear_init_auth_failure_state("qr-unit")
        XianyuLive._instances.pop("qr-unit", None)
        XianyuLive._post_qr_patrol_due.pop("qr-unit", None)

    def test_clear_all_login_backoffs_resets_instance_state(self):
        live = _live()
        XianyuLive.set_password_login_failure_backoff("qr-unit", "hard_block", 3600)
        XianyuLive.record_init_auth_failure("qr-unit", "boom")

        self.assertFalse(live._login_backoff_wake_event.is_set())
        live.clear_all_login_backoffs()

        self.assertIsNone(XianyuLive.get_password_login_failure_backoff("qr-unit"))
        self.assertIsNone(XianyuLive.get_init_auth_failure_state("qr-unit"))
        self.assertIsNone(live.last_token_refresh_status)
        self.assertEqual(live.last_token_refresh_error_message, "")
        self.assertEqual(live.connection_failures, 0)
        self.assertEqual(live.init_auth_failures, 0)
        self.assertTrue(live._login_backoff_wake_event.is_set())

    def test_clear_class_level_backoffs_covers_registered_instance(self):
        live = _live()
        XianyuLive._instances["qr-unit"] = live
        XianyuLive.set_password_login_failure_backoff("qr-unit", "slider_failed", 600)
        XianyuLive.record_init_auth_failure("qr-unit", "boom")

        XianyuLive.clear_class_level_backoffs("qr-unit")

        self.assertIsNone(XianyuLive.get_password_login_failure_backoff("qr-unit"))
        self.assertIsNone(XianyuLive.get_init_auth_failure_state("qr-unit"))
        self.assertIsNone(live.last_token_refresh_status)
        self.assertEqual(live.connection_failures, 0)
        self.assertEqual(live.init_auth_failures, 0)

    def test_clear_class_level_backoffs_ignores_unknown_account(self):
        # 不应抛错，也不应影响其他账号
        XianyuLive.set_password_login_failure_backoff("other-unit", "hard_block", 600)
        try:
            XianyuLive.clear_class_level_backoffs("qr-unit")
            self.assertIsNotNone(XianyuLive.get_password_login_failure_backoff("other-unit"))
        finally:
            XianyuLive.clear_password_login_failure_backoff("other-unit")


class ManualQrWaitTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive.clear_password_login_failure_backoff("qr-unit")

    def test_no_backoff_means_no_wait(self):
        live = _live()
        self.assertEqual(live._get_manual_qr_wait_seconds(), 0)

    def test_hard_block_backoff_waits_in_low_frequency_window(self):
        live = _live()
        XianyuLive.set_password_login_failure_backoff("qr-unit", "hard_block", 3600)
        wait_seconds = live._get_manual_qr_wait_seconds()
        self.assertGreaterEqual(wait_seconds, 300)
        self.assertLessEqual(wait_seconds, 600)

    def test_short_backoff_waits_at_least_five_minutes(self):
        live = _live()
        XianyuLive.set_password_login_failure_backoff("qr-unit", "slider_failed", 60)
        wait_seconds = live._get_manual_qr_wait_seconds()
        self.assertGreaterEqual(wait_seconds, 300)
        self.assertLessEqual(wait_seconds, 600)

    def test_expired_backoff_does_not_wait(self):
        live = _live()
        XianyuLive._password_login_failure_backoff["qr-unit"] = {
            "until": time.time() - 1,
            "reason": "hard_block",
            "seconds": 600,
        }
        self.assertEqual(live._get_manual_qr_wait_seconds(), 0)


class PostQrPatrolTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._post_qr_patrol_due.pop("qr-unit", None)

    def test_patrol_request_consumed_once(self):
        XianyuLive.request_post_qr_delivery_patrol("qr-unit")
        self.assertTrue(XianyuLive.consume_post_qr_delivery_patrol_request("qr-unit"))
        self.assertFalse(XianyuLive.consume_post_qr_delivery_patrol_request("qr-unit"))

    def test_patrol_not_ready_re_registers_request(self):
        live = _live()
        live.current_token = None
        live.connection_state = ConnectionState.RECONNECTING

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", side_effect=AssertionError("db")), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 0)
        # 连接未就绪时不能把登记丢掉：必须放回，等下次连接就绪再触发
        self.assertIn("qr-unit", XianyuLive._post_qr_patrol_due)

    def test_patrol_routes_pending_platform_confirm_without_resend(self):
        live = _live()
        live.current_token = "token"
        live.connection_state = ConnectionState.CONNECTED
        pending = [{"order_id": "o1", "item_id": "i", "buyer_id": "b", "sid": "s@goofish",
                    "order_status": "partial_pending_finalize"}]
        compensate_calls = []

        async def fake_compensate(order, source="post_qr_patrol"):
            compensate_calls.append(order["order_id"])
            return True

        async def forbid_full(*args, **kwargs):
            raise AssertionError("全部单元已送达时应走差异化收尾，不应走全量补发")

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", return_value=pending), \
                mock.patch.object(XianyuLive, "_get_order_expected_delivery_quantity", return_value=1), \
                mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                                  return_value={"remaining_unit_indexes": [],
                                                "pending_finalize_unit_indexes": [1],
                                                "finalized_unit_indexes": []}), \
                mock.patch.object(XianyuLive, "_compensate_partial_order", side_effect=fake_compensate), \
                mock.patch.object(XianyuLive, "_auto_deliver_recovered_pending_order", side_effect=forbid_full), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 1)
        self.assertEqual(compensate_calls, ["o1"])

    def test_patrol_routes_remaining_units_to_compensation(self):
        live = _live()
        live.current_token = "token"
        live.connection_state = ConnectionState.CONNECTED
        live.ws = "ws"
        pending = [{"order_id": "o2", "item_id": "i", "buyer_id": "b", "sid": "s@goofish",
                    "order_status": "partial_success"}]
        compensate_calls = []

        async def fake_compensate(order, source="post_qr_patrol"):
            compensate_calls.append((order["order_id"], source))
            return True

        async def forbid_full(*args, **kwargs):
            raise AssertionError("有会话上下文时应走差异化补发，不应退回单件恢复路径")

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", return_value=pending), \
                mock.patch.object(XianyuLive, "_get_order_expected_delivery_quantity", return_value=3), \
                mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                                  return_value={"remaining_unit_indexes": [2, 3],
                                                "pending_finalize_unit_indexes": [],
                                                "finalized_unit_indexes": [1]}), \
                mock.patch.object(XianyuLive, "_compensate_partial_order", side_effect=fake_compensate), \
                mock.patch.object(XianyuLive, "_auto_deliver_recovered_pending_order", side_effect=forbid_full), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 1)
        self.assertEqual(compensate_calls, [("o2", "post_qr_patrol")])

    def test_patrol_delivers_pending_orders_with_fulfillment_priority(self):
        live = _live()
        live.current_token = "token"
        live.connection_state = ConnectionState.CONNECTED
        pending = [
            {"order_id": "o1", "order_status": "pending_ship"},
            {"order_id": "o2", "order_status": "pending_ship"},
        ]
        delivered = []

        async def fake_deliver(order, *, fallback_order=None, source="order_recovery"):
            delivered.append((order["order_id"], source))
            return True

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", return_value=pending) as gp, \
                mock.patch.object(XianyuLive, "_auto_deliver_recovered_pending_order", side_effect=fake_deliver), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 2)
        self.assertEqual(delivered, [("o1", "post_qr_patrol"), ("o2", "post_qr_patrol")])
        self.assertEqual(gp.call_args.args, ("qr-unit", 20))
        self.assertEqual(live._fulfillment_priority_depth, 0)


def _partial_live():
    live = _live()
    live.current_token = "token"
    live.connection_state = ConnectionState.CONNECTED
    live.ws = "ws"
    live._safe_str = str
    live._build_delivery_steps = lambda content, desc="": [{"type": "text", "content": content}]
    live._record_delivery_log = mock.MagicMock()
    live._persist_delivery_finalization_state = mock.MagicMock(return_value=True)
    live._sync_order_delivery_progress = mock.MagicMock()
    live._mark_data_reservation_sent_if_needed = mock.MagicMock(return_value=True)
    live._release_data_reservation_if_needed = mock.MagicMock(return_value=True)
    live._get_order_expected_delivery_quantity = mock.MagicMock(return_value=3)
    return live


def _summary(finalized, pending_finalize, remaining):
    return {
        "expected_quantity": 3,
        "finalized_unit_indexes": list(finalized),
        "pending_finalize_unit_indexes": list(pending_finalize),
        "remaining_unit_indexes": list(remaining),
        "aggregate_status": "partial_success",
        "states": [],
    }


class PartialCompensationTests(unittest.TestCase):
    ORDER = {"order_id": "po1", "item_id": "it1", "buyer_id": "b1", "buyer_nick": "买家", "sid": "sid1@goofish", "order_status": "partial_success"}

    def test_only_remaining_units_are_sent_and_finalized(self):
        live = _partial_live()
        auto_calls = []

        async def fake_auto_delivery(item_id, title, order_id, buyer_id, chat_id, nick=None,
                                     include_meta=False, delivery_unit_index=1):
            auto_calls.append(delivery_unit_index)
            return {"content": f"card-{delivery_unit_index}", "delivery_steps": [{"type": "text", "content": "x"}],
                    "data_reservation_id": f"r{delivery_unit_index}", "delivery_unit_index": delivery_unit_index}

        async def fake_send(websocket, chat_id, buyer_id, steps, **kwargs):
            return None

        async def fake_finalize(delivery_meta=None, order_id=None, item_id=None, skip_confirm=False, force_confirm=False):
            return {"success": True}

        async def fake_retry(order_id=None, limit=50, source="manual"):
            return {"success": True, "processed": 0, "confirmed": 0, "failed": 0}

        state_map = {}  # 单元无状态 → remaining
        with mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                               return_value=_summary([1], [], [2, 3])), \
             mock.patch("db_manager.db_manager.get_delivery_finalization_state",
                        side_effect=lambda oid, unit: state_map.get(unit)), \
             mock.patch.object(XianyuLive, "_auto_delivery", side_effect=fake_auto_delivery), \
             mock.patch.object(XianyuLive, "_send_delivery_steps", side_effect=fake_send), \
             mock.patch.object(XianyuLive, "_finalize_delivery_after_send", side_effect=fake_finalize), \
             mock.patch.object(XianyuLive, "retry_pending_platform_confirms", side_effect=fake_retry), \
             mock.patch.object(XianyuLive, "_get_pending_delivery_finalization_meta",
                               side_effect=lambda oid, unit: {"delivery_unit_index": unit, "success": True}):
            ok = asyncio.run(live._compensate_partial_order(dict(self.ORDER), source="post_qr_patrol"))

        self.assertTrue(ok)
        # 只补发单元 2/3，已完成单元 1 绝不重发
        self.assertEqual(auto_calls, [2, 3])
        # 全部送达后统一收尾
        self.assertEqual(live._persist_delivery_finalization_state.call_count, 4)  # 2 sent + 2 finalized

    def test_hard_gate_skips_units_with_existing_state(self):
        live = _partial_live()

        async def forbid_auto(*args, **kwargs):
            raise AssertionError("硬闸门应拦截已有状态的单元，绝不重发")

        async def fake_retry(order_id=None, limit=50, source="manual"):
            return {"success": True, "processed": 0, "confirmed": 0, "failed": 0}

        async def fake_finalize(**kwargs):
            return {"success": True}

        with mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                               return_value=_summary([1], [], [2, 3])), \
             mock.patch("db_manager.db_manager.get_delivery_finalization_state",
                        side_effect=lambda oid, unit: {"status": "finalized"} if unit == 2 else {"status": "sent"}), \
             mock.patch.object(XianyuLive, "_auto_delivery", side_effect=forbid_auto), \
             mock.patch.object(XianyuLive, "retry_pending_platform_confirms", side_effect=fake_retry), \
             mock.patch.object(XianyuLive, "_finalize_delivery_after_send", side_effect=fake_finalize), \
             mock.patch.object(XianyuLive, "_get_pending_delivery_finalization_meta", return_value=None):
            ok = asyncio.run(live._compensate_partial_order(dict(self.ORDER), source="post_qr_patrol"))

        self.assertTrue(ok)

    def test_send_failure_blocks_platform_confirm(self):
        live = _partial_live()
        sent_units = []

        async def fake_auto_delivery(item_id, title, order_id, buyer_id, chat_id, nick=None,
                                     include_meta=False, delivery_unit_index=1):
            return {"content": f"card-{delivery_unit_index}",
                    "delivery_steps": [{"type": "text", "content": f"card-{delivery_unit_index}"}]}

        async def flaky_send(websocket, chat_id, buyer_id, steps, **kwargs):
            # 单元2发送成功，单元3回执超时
            if "card-3" not in str(steps):
                return None
            raise RuntimeError("ack timeout")

        async def forbid_finalize(**kwargs):
            raise AssertionError("存在未送达单元时绝不允许触发整单平台确认")

        async def forbid_retry(order_id=None, limit=50, source="manual"):
            raise AssertionError("存在未送达单元时不允许补确认")

        with mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                               return_value=_summary([], [], [2, 3])), \
             mock.patch("db_manager.db_manager.get_delivery_finalization_state", return_value=None), \
             mock.patch.object(XianyuLive, "_auto_delivery", side_effect=fake_auto_delivery), \
             mock.patch.object(XianyuLive, "_send_delivery_steps", side_effect=flaky_send), \
             mock.patch.object(XianyuLive, "_finalize_delivery_after_send", side_effect=forbid_finalize), \
             mock.patch.object(XianyuLive, "retry_pending_platform_confirms", side_effect=forbid_retry), \
             mock.patch.object(XianyuLive, "_get_pending_delivery_finalization_meta", return_value=None):
            ok = asyncio.run(live._compensate_partial_order(dict(self.ORDER), source="post_qr_patrol"))

        self.assertFalse(ok)
        # 已送达的单元2保持 sent（带 hold 错误），不 finalize、不释放
        statuses = [call.kwargs.get("status") for call in live._persist_delivery_finalization_state.call_args_list]
        self.assertEqual(statuses.count("sent"), 2)  # 送达标记一次 + hold 一次
        self.assertNotIn("finalized", statuses)

    def test_pending_finalize_units_are_never_resent(self):
        live = _partial_live()
        finalize_units = []

        async def forbid_auto(*args, **kwargs):
            raise AssertionError("待收尾单元私信已送达，严禁重发")

        async def fake_finalize(delivery_meta=None, order_id=None, item_id=None, **kwargs):
            finalize_units.append(delivery_meta.get("delivery_unit_index"))
            return {"success": True}

        async def fake_retry(order_id=None, limit=50, source="manual"):
            return {"success": True, "processed": 0, "confirmed": 0, "failed": 0}

        with mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                               return_value=_summary([3], [1, 2], [])), \
             mock.patch.object(XianyuLive, "_auto_delivery", side_effect=forbid_auto), \
             mock.patch.object(XianyuLive, "_finalize_delivery_after_send", side_effect=fake_finalize), \
             mock.patch.object(XianyuLive, "retry_pending_platform_confirms", side_effect=fake_retry), \
             mock.patch.object(XianyuLive, "_get_pending_delivery_finalization_meta",
                               side_effect=lambda oid, unit: {"delivery_unit_index": unit, "success": True}):
            ok = asyncio.run(live._compensate_partial_order(dict(self.ORDER), source="post_qr_patrol"))

        self.assertTrue(ok)
        self.assertEqual(sorted(finalize_units), [1, 2])

    def test_pending_platform_confirm_units_route_to_retry_only(self):
        live = _partial_live()
        retry_calls = []

        async def forbid_finalize(**kwargs):
            raise AssertionError("pending_platform_confirm 单元应交给专用补确认通道")

        async def fake_retry(order_id=None, limit=50, source="manual"):
            retry_calls.append(order_id)
            return {"success": True, "processed": 1, "confirmed": 1, "failed": 0}

        with mock.patch.object(XianyuLive, "_summarize_delivery_progress",
                               return_value=_summary([3], [1], [])), \
             mock.patch.object(XianyuLive, "_auto_delivery", side_effect=AssertionError("no send")), \
             mock.patch.object(XianyuLive, "_finalize_delivery_after_send", side_effect=forbid_finalize), \
             mock.patch.object(XianyuLive, "retry_pending_platform_confirms", side_effect=fake_retry), \
             mock.patch.object(XianyuLive, "_get_pending_delivery_finalization_meta",
                               side_effect=lambda oid, unit: {"delivery_unit_index": unit, "success": True,
                                                              "pending_platform_confirm": True}):
            ok = asyncio.run(live._compensate_partial_order(dict(self.ORDER), source="post_qr_patrol"))

        self.assertTrue(ok)
        self.assertEqual(retry_calls, ["po1"])


class HardBlockTokenSkipTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive.clear_password_login_failure_backoff("qr-unit")

    def test_hard_block_backoff_skips_token_refresh(self):
        live = _live()
        XianyuLive.set_password_login_failure_backoff("qr-unit", "hard_block", 3600)
        self.assertTrue(live._should_skip_token_refresh_for_login_backoff())
        self.assertEqual(live.last_token_refresh_status, "password_login_backoff_wait")

    def test_no_backoff_does_not_skip(self):
        live = _live()
        self.assertFalse(live._should_skip_token_refresh_for_login_backoff())


class QrHotReloadTests(unittest.TestCase):
    def test_auto_pause_note_predicate(self):
        from reply_server import _is_auto_pause_status_note
        self.assertTrue(_is_auto_pause_status_note('连续风控保护中'))
        self.assertTrue(_is_auto_pause_status_note('风控保护中'))
        self.assertTrue(_is_auto_pause_status_note('账号风控保护中'))
        self.assertTrue(_is_auto_pause_status_note('待人脸验证'))
        self.assertTrue(_is_auto_pause_status_note('待二维码验证'))
        self.assertTrue(_is_auto_pause_status_note('待扫码登录'))
        # 手动停用（无文案）或运营自定义文案：不允许扫码自动恢复
        self.assertFalse(_is_auto_pause_status_note(''))
        self.assertFalse(_is_auto_pause_status_note('手动停用观察中'))
        self.assertFalse(_is_auto_pause_status_note('卷入售后纠纷暂停'))

    def test_manually_disabled_account_is_not_resumed_by_qr(self):
        import reply_server

        with mock.patch.object(reply_server.db_manager, 'get_cookie_details',
                               return_value={'status_note': ''}), \
             mock.patch.object(reply_server.db_manager, 'get_cookie_status', return_value=False), \
             mock.patch.object(reply_server.db_manager, 'save_cookie_status') as save_status, \
             mock.patch.object(reply_server.db_manager, 'update_cookie_status_note') as clear_note:
            reply_server._clear_login_backoffs_for_qr_success('manual-disabled', None)

        save_status.assert_not_called()
        clear_note.assert_not_called()

    def test_auto_paused_account_is_resumed_by_qr(self):
        import reply_server

        with mock.patch.object(reply_server.db_manager, 'get_cookie_details',
                               return_value={'status_note': '连续风控保护中'}), \
             mock.patch.object(reply_server.db_manager, 'get_cookie_status', return_value=False), \
             mock.patch.object(reply_server.db_manager, 'save_cookie_status') as save_status, \
             mock.patch.object(reply_server.db_manager, 'update_cookie_status_note') as clear_note:
            reply_server._clear_login_backoffs_for_qr_success('auto-paused', None)

        save_status.assert_called_once_with('auto-paused', True)
        clear_note.assert_called_once_with('auto-paused', '')

    def test_hot_reload_clears_backoffs_before_and_after_switch(self):
        import reply_server

        clear_calls = []
        manager = mock.MagicMock()
        manager.get_xianyu_instance.return_value = None

        async def fake_update(cid, value, save_to_db=True):
            # 模拟旧实例在取消交接期间写回退避的时点
            XianyuLive.set_password_login_failure_backoff('race-acct', 'hard_block', 3600)
            return None

        manager.update_cookie.side_effect = fake_update

        with mock.patch.object(reply_server.cookie_manager, 'manager', manager), \
             mock.patch('XianyuAutoAsync.XianyuLive.clear_class_level_backoffs',
                        side_effect=lambda cid: clear_calls.append(cid)), \
             mock.patch('XianyuAutoAsync.XianyuLive.request_post_qr_delivery_patrol') as request_patrol, \
             mock.patch.object(reply_server.db_manager, 'get_cookie_details', return_value={'status_note': ''}), \
             mock.patch.object(reply_server.db_manager, 'get_cookie_status', return_value=True), \
             mock.patch.object(reply_server.db_manager, 'update_cookie_status_note'):
            ok = asyncio.run(reply_server._hot_reload_after_qr_success(
                'race-acct', 'cookie-value', is_new_account=False, current_user=None,
            ))

        self.assertTrue(ok)
        # 切换前一次 + 切换后一次：交接期间写回的退避也会被第二次清掉
        self.assertEqual(clear_calls.count('race-acct'), 2)
        request_patrol.assert_called_once_with('race-acct')


if __name__ == "__main__":
    unittest.main()

    def test_patrol_skips_when_connection_not_ready(self):
        live = _live()
        live.current_token = None
        live.connection_state = ConnectionState.RECONNECTING

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", side_effect=AssertionError("db")), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 0)
        self.assertEqual(live._fulfillment_priority_depth, 0)

    def test_patrol_survives_single_order_failure(self):
        live = _live()
        live.current_token = "token"
        live.connection_state = ConnectionState.CONNECTED
        pending = [{"order_id": "o1", "order_status": "pending_ship"}]
        live._safe_str = str

        async def failing_deliver(order, *, fallback_order=None, source="order_recovery"):
            raise RuntimeError("send failed")

        async def noop_sleep(_delay, *args, **kwargs):
            return None

        with mock.patch("db_manager.db_manager.get_pending_ship_orders", return_value=pending), \
                mock.patch.object(XianyuLive, "_auto_deliver_recovered_pending_order", side_effect=failing_deliver), \
                mock.patch.object(asyncio, "sleep", noop_sleep):
            fulfilled = asyncio.run(live._run_post_qr_delivery_patrol())

        self.assertEqual(fulfilled, 0)
        self.assertEqual(live._fulfillment_priority_depth, 0)

    def test_connected_hook_consumes_request(self):
        # main() 连接就绪时消费巡检登记，不重复触发
        XianyuLive.request_post_qr_delivery_patrol("qr-unit")
        live = _live()
        self.assertTrue(live.consume_post_qr_delivery_patrol_request(live.cookie_id))
        self.assertFalse(live.consume_post_qr_delivery_patrol_request(live.cookie_id))


if __name__ == "__main__":
    unittest.main()
