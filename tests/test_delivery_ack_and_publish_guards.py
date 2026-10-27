import asyncio
import json
import time
import unittest
from unittest import mock

import XianyuAutoAsync
from XianyuAutoAsync import MessageSendAckTimeoutError, XianyuLive, _send_ack_is_success
from utils.item_publisher import ItemPublisher, sanitize_publish_text
from utils.xianyu_slider_stealth import HardBlockCircuitError, feedback_is_hard_block


class _SharedSocket:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)


class _DedicatedSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        if not self.frames:
            await asyncio.sleep(5)
        frame = self.frames.pop(0)
        if callable(frame):
            frame = frame(self)
        return frame


def _live():
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = "ack-unit"
    live.myid = "seller-1"
    live.lwp_response_waiters = {}
    live._fulfillment_priority_depth = 0
    live.last_token_refresh_status = ""
    live.last_token_refresh_error_message = ""
    live._ready_websockets = set()
    return live


def _ready(live, socket):
    """回执测试只覆盖送达确认；连接必须先登记为业务就绪，否则发送守卫会直接拒绝。"""
    live._ready_websockets.add(socket)
    return socket


class SendAckTests(unittest.TestCase):
    def test_success_code_must_be_200(self):
        self.assertTrue(_send_ack_is_success({"code": 200}))
        self.assertTrue(_send_ack_is_success({"headers": {"code": "200"}}))
        self.assertFalse(_send_ack_is_success({"headers": {"mid": "m1"}}))
        self.assertFalse(_send_ack_is_success({"code": 400}))
        self.assertFalse(_send_ack_is_success("ok"))

    def test_shared_socket_waits_for_matching_ack(self):
        live = _live()
        socket = _ready(live, _SharedSocket())
        live.ws = socket

        async def scenario():
            async def answer():
                while not socket.sent or not live.lwp_response_waiters:
                    await asyncio.sleep(0)
                mid = json.loads(socket.sent[0])["headers"]["mid"]
                live.lwp_response_waiters[mid].set_result({"code": 200, "headers": {"mid": mid}})

            waiter = asyncio.create_task(answer())
            result = await live.send_msg(socket, "cid", "buyer", "链接")
            await waiter
            return result

        self.assertTrue(asyncio.run(scenario()))

    def test_shared_socket_error_receipt_is_not_success(self):
        live = _live()
        socket = _ready(live, _SharedSocket())
        live.ws = socket

        async def scenario():
            async def answer():
                while not socket.sent or not live.lwp_response_waiters:
                    await asyncio.sleep(0)
                mid = json.loads(socket.sent[0])["headers"]["mid"]
                live.lwp_response_waiters[mid].set_result({"code": 500, "headers": {"mid": mid}})

            asyncio.create_task(answer())
            await live.send_msg(socket, "cid", "buyer", "链接")

        with self.assertRaises(MessageSendAckTimeoutError):
            asyncio.run(scenario())

    def test_shared_socket_timeout_raises(self):
        live = _live()
        socket = _ready(live, _SharedSocket())
        live.ws = socket

        async def scenario():
            with mock.patch.object(XianyuAutoAsync, "SEND_ACK_TIMEOUT_SECONDS", 0.05):
                await live.send_msg(socket, "cid", "buyer", "链接")

        with self.assertRaises(MessageSendAckTimeoutError):
            asyncio.run(scenario())

    def test_dedicated_socket_skips_unrelated_frame_then_accepts_ack(self):
        live = _live()
        live.ws = _SharedSocket()

        def success_frame(socket):
            mid = json.loads(socket.sent[0])["headers"]["mid"]
            return json.dumps({"code": 200, "headers": {"mid": mid}})

        socket = _ready(live, _DedicatedSocket([
            json.dumps({"code": 200, "headers": {"mid": "other"}}),
            success_frame,
        ]))

        self.assertTrue(asyncio.run(live.send_msg(socket, "cid", "buyer", "链接")))

    def test_dedicated_socket_error_receipt_raises(self):
        live = _live()
        live.ws = _SharedSocket()

        def error_frame(socket):
            mid = json.loads(socket.sent[0])["headers"]["mid"]
            return json.dumps({"code": 400, "headers": {"mid": mid}})

        socket = _ready(live, _DedicatedSocket([error_frame]))
        with self.assertRaises(MessageSendAckTimeoutError):
            asyncio.run(live.send_msg(socket, "cid", "buyer", "链接"))


class FulfillmentGraceTests(unittest.TestCase):
    def _grace_live(self, pending):
        live = _live()
        live._get_qr_login_grace_until = lambda: time.time() + 600
        live._account_has_pending_shipment = lambda: pending
        return live

    def test_grace_still_defers_background_recovery(self):
        live = self._grace_live(False)
        self.assertTrue(live._should_defer_auth_recovery_for_qr_grace(time.time()))

    def test_pending_shipment_depth_and_explicit_flag_bypass_grace(self):
        now = time.time()
        pending = self._grace_live(True)
        self.assertFalse(pending._should_defer_auth_recovery_for_qr_grace(now))

        flagged = self._grace_live(False)
        self.assertFalse(flagged._should_defer_auth_recovery_for_qr_grace(now, allow_fulfillment=True))

        busy = self._grace_live(False)
        busy._fulfillment_priority_depth = 1
        self.assertFalse(busy._should_defer_auth_recovery_for_qr_grace(now))


class PublishGuardTests(unittest.TestCase):
    def test_sensitive_word_and_default_quantity(self):
        self.assertEqual(sanitize_publish_text("直通课程"), "系统进阶课程")
        publisher = ItemPublisher("unb=1; _m_h5_tk=tok_1", "acct")
        payload = publisher._build_publish_payload(
            title="标题",
            description="描述",
            uploaded_images=[{"url": "https://img.example/a.jpg", "height": 10, "width": 10}],
            channel_res={"data": {"cardList": [], "categoryPredictResult": {
                "catId": "1", "catName": "类目", "channelCatId": "2", "tbCatId": "3",
            }}},
            location={
                "area": "", "city": "", "divisionId": 0, "longitude": 0, "latitude": 0,
                "poiId": "", "poi": "", "prov": "",
            },
            current_price=9.9,
            original_price=None,
            delivery_choice="无需邮寄",
            post_price=None,
            can_self_pickup=False,
        )
        self.assertEqual(payload["quantity"], "999")

    def test_edit_quantity_refuses_incomplete_item_before_post(self):
        publisher = ItemPublisher("unb=1; _m_h5_tk=tok_1", "acct")

        async def fail_if_posted(*_args, **_kwargs):
            raise AssertionError("缺字段时不应请求编辑接口")

        publisher._post_mtop = fail_if_posted
        with self.assertRaises(ValueError):
            asyncio.run(publisher.edit_item_quantity(
                item_id="10001",
                item_fields={"title": "标题", "price": 1, "categoryId": "9"},
            ))
        with self.assertRaises(ValueError):
            asyncio.run(publisher.edit_item_quantity(
                item_id="10001",
                item_fields={
                    "title": "标题",
                    "price": 1,
                    "images": ["https://img.example/a.jpg"],
                },
            ))


class CaptchaCircuitTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive.clear_password_login_failure_backoff("ack-unit")

    def test_hard_block_feedback_does_not_match_recoverable_shell(self):
        self.assertTrue(feedback_is_hard_block("RGV587_ERROR"))
        self.assertTrue(feedback_is_hard_block({"url": "https://x/punish?pureCaptcha=true"}))
        self.assertFalse(feedback_is_hard_block({"url": "https://x/punish?pureCaptcha="}))
        self.assertFalse(feedback_is_hard_block("FAIL_SYS_USER_VALIDATE"))

    def test_disabled_recovery_does_not_open_browser(self):
        live = _live()
        live._is_auto_captcha_recovery_enabled = lambda: False

        async def scenario():
            with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", side_effect=AssertionError("browser")):
                return await live._handle_captcha_verification({"data": {"url": "https://example.com/captcha"}})

        self.assertIsNone(asyncio.run(scenario()))
        self.assertEqual(live.last_token_refresh_status, "captcha_recovery_disabled")
        self.assertIsNone(XianyuLive.get_password_login_failure_backoff("ack-unit"))

    def test_hard_block_response_raises_before_browser(self):
        live = _live()

        async def scenario():
            with mock.patch("utils.xianyu_slider_stealth.XianyuSliderStealth", side_effect=AssertionError("browser")):
                await live._handle_captcha_verification({
                    "ret": ["FAIL_SYS_USER_VALIDATE::RGV587_ERROR"],
                    "data": {"url": "https://x/punish?pureCaptcha=true"},
                })

        with self.assertRaises(HardBlockCircuitError):
            asyncio.run(scenario())


class ImageSendAckTests(unittest.TestCase):
    def test_shared_socket_image_waits_for_ack(self):
        live = _live()
        live._safe_str = str
        socket = _ready(live, _SharedSocket())
        live.ws = socket

        async def scenario():
            async def answer():
                while not socket.sent or not live.lwp_response_waiters:
                    await asyncio.sleep(0)
                mid = json.loads(socket.sent[0])["headers"]["mid"]
                live.lwp_response_waiters[mid].set_result({"code": 200, "headers": {"mid": mid}})

            waiter = asyncio.create_task(answer())
            await live.send_image_msg(socket, "cid", "buyer", "https://img.alicdn.com/a.jpg")
            await waiter

        asyncio.run(scenario())
        sent = json.loads(socket.sent[0])
        self.assertEqual(sent["lwp"], "/r/MessageSend/sendByReceiverScope")
        self.assertIn("mid", sent["headers"])

    def test_shared_socket_image_timeout_and_error_are_not_success(self):
        live = _live()
        live._safe_str = str
        socket = _ready(live, _SharedSocket())
        live.ws = socket

        async def timeout_scenario():
            with mock.patch.object(XianyuAutoAsync, "SEND_ACK_TIMEOUT_SECONDS", 0.05):
                await live.send_image_msg(socket, "cid", "buyer", "https://img.alicdn.com/a.jpg")

        with self.assertRaises(MessageSendAckTimeoutError):
            asyncio.run(timeout_scenario())

        error_socket = _ready(live, _SharedSocket())
        live.ws = error_socket

        async def error_scenario():
            async def answer():
                while not error_socket.sent or not live.lwp_response_waiters:
                    await asyncio.sleep(0)
                mid = json.loads(error_socket.sent[0])["headers"]["mid"]
                live.lwp_response_waiters[mid].set_result({"code": 500, "headers": {"mid": mid}})

            asyncio.create_task(answer())
            await live.send_image_msg(error_socket, "cid", "buyer", "https://img.alicdn.com/a.jpg")

        with self.assertRaises(MessageSendAckTimeoutError):
            asyncio.run(error_scenario())


class PasswordLoginRemovedTests(unittest.TestCase):
    def test_password_refresh_never_constructs_slider(self):
        live = _live()

        async def scenario():
            with mock.patch(
                "utils.xianyu_slider_stealth.XianyuSliderStealth",
                side_effect=AssertionError("browser"),
            ):
                return await live._try_password_login_refresh("unit")

        self.assertFalse(asyncio.run(scenario()))
        self.assertEqual(live.last_token_refresh_status, "password_login_removed")

    def test_password_methods_return_without_browser(self):
        from utils.xianyu_slider_stealth import XianyuSliderStealth

        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        self.assertIsNone(slider.login_with_password_playwright("acct", "secret", show_browser=True))
        self.assertIsNone(slider.login_with_password_headful("acct", "secret", True))
        self.assertEqual(slider.last_login_error, "密码重登已关闭，请使用扫码登录")

    def test_password_patch_does_not_reinstall_browser_login(self):
        import inspect

        from utils import slider_patch
        from utils.xianyu_slider_stealth import XianyuSliderStealth

        source = inspect.getsource(slider_patch.patch_login_with_password_headful)
        self.assertNotIn("chromium", source)
        self.assertNotIn("password-login", source)
        self.assertTrue(slider_patch.patch_login_with_password_headful())
        slider = XianyuSliderStealth.__new__(XianyuSliderStealth)
        self.assertIsNone(slider.login_with_password_headful("acct", "secret", True))
        executor_source = inspect.getsource(XianyuLive._try_password_login_refresh)
        self.assertNotIn("XianyuSliderStealth", executor_source)


if __name__ == "__main__":
    unittest.main()
