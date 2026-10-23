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
