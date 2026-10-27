import asyncio
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from db_manager import DBManager
from XianyuAutoAsync import XianyuLive
import reply_server


STORED = "unb=1; cookie2=c; _m_h5_tk=t; extra=1"
REORDERED = "_m_h5_tk=t; extra=9; unb=1; cookie2=c"
OTHER_COOKIE2 = "unb=1; cookie2=OTHER; _m_h5_tk=t"
OTHER_UNB = "unb=2; cookie2=c; _m_h5_tk=t"
INCOMPLETE = "unb=1; cookie2=c"
INCOMPLETE_REORDERED = "cookie2=c; unb=1"


def _budget_store():
    state = {"attempts": 0, "hold_until": 0, "hold_reason": "", "grace_rechecked": 0}

    def get_budget(_cookie_id):
        return dict(state)

    def set_budget(_cookie_id, attempts, hold_until, hold_reason=""):
        state["attempts"] = attempts
        state["hold_until"] = hold_until
        state["hold_reason"] = hold_reason
        return True

    def clear_budget(_cookie_id):
        state["attempts"] = 0
        state["hold_until"] = 0
        state["hold_reason"] = ""
        return True

    return state, get_budget, set_budget, clear_budget


def _live(cookie_id):
    live = XianyuLive.__new__(XianyuLive)
    live.cookie_id = cookie_id
    live._last_token_recovery_plan = None
    live.send_token_refresh_notification = mock.AsyncMock()
    return live


class RecoveryPlanTests(unittest.TestCase):
    def test_backoff_sequence_stops_on_fifth_attempt(self):
        seen = []
        for attempt in range(1, 6):
            plan = XianyuLive.plan_token_recovery_failure(
                attempt,
                now=1000,
                schedule=[600, 1800, 7200],
                max_attempts=5,
                hold_seconds=21600,
            )
            seen.append((plan["wait_seconds"], plan["stop"]))
        self.assertEqual(seen, [(600, False), (1800, False), (7200, False), (7200, False), (0, True)])
        sixth = XianyuLive.plan_token_recovery_failure(
            6, now=1000, schedule=[600, 1800, 7200], max_attempts=5, hold_seconds=21600
        )
        self.assertTrue(sixth["stop"])
        self.assertEqual(sixth["hold_until"], 1000 + 21600)

    def test_platform_punish_stops_on_first_attempt(self):
        plan = XianyuLive.plan_token_recovery_failure(
            1, terminal=True, now=1000, schedule=[600, 1800, 7200], max_attempts=5, hold_seconds=21600
        )
        self.assertEqual(plan["wait_seconds"], 0)
        self.assertTrue(plan["stop"])
        self.assertTrue(plan["terminal"])
        self.assertEqual(plan["hold_until"], 1000 + 21600)


class RecoveryBudgetTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._auth_prewarmed_tokens.pop("budget-acc", None)
        XianyuLive._verified_chat_handoffs.pop("budget-acc", None)

    def test_fifth_failure_stops_further_http(self):
        live = _live("budget-acc")
        _state, get_budget, set_budget, clear_budget = _budget_store()

        async def scenario():
            calls = 0
            while calls < 8:
                if live._token_recovery_hold_active():
                    break
                calls += 1
                await live._record_token_probe_failure("session_expired")
            return calls

        with mock.patch("XianyuAutoAsync.db_manager.get_token_recovery_budget", get_budget), \
                mock.patch("XianyuAutoAsync.db_manager.set_token_recovery_budget", set_budget), \
                mock.patch("XianyuAutoAsync.db_manager.clear_token_recovery_budget", clear_budget):
            calls = asyncio.run(scenario())
            self.assertEqual(calls, 5)
            self.assertTrue(live._token_recovery_hold_active())
            self.assertEqual(live.last_token_refresh_status, "token_recovery_manual_hold")
            self.assertNotIn("账号离线", live.last_token_refresh_error_message)
            self.assertNotIn("无法登录", live.last_token_refresh_error_message)
            live.send_token_refresh_notification.assert_awaited()

    def test_punish_response_terminates_immediately(self):
        live = _live("budget-acc")
        _state, get_budget, set_budget, clear_budget = _budget_store()

        async def scenario():
            calls = 0
            if not live._token_recovery_hold_active():
                calls += 1
                await live._record_token_probe_failure("platform_punish", terminal=True)
            if not live._token_recovery_hold_active():
                calls += 1
                await live._record_token_probe_failure("platform_punish", terminal=True)
            return calls

        with mock.patch("XianyuAutoAsync.db_manager.get_token_recovery_budget", get_budget), \
                mock.patch("XianyuAutoAsync.db_manager.set_token_recovery_budget", set_budget), \
                mock.patch("XianyuAutoAsync.db_manager.clear_token_recovery_budget", clear_budget):
            calls = asyncio.run(scenario())
            self.assertEqual(calls, 1)
            self.assertTrue(live._token_recovery_hold_active())
            self.assertEqual(live.last_token_refresh_status, "platform_risk_hold")
            self.assertIn("重复扫码无效", live.last_token_refresh_error_message)
            self.assertNotIn("账号离线", live.last_token_refresh_error_message)


class HandoffIdentityTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._verified_chat_handoffs.pop("handoff-acc", None)
        XianyuLive._chat_auth_generations.pop("handoff-acc", None)

    def test_reordered_cookie_authorizes_and_consumes(self):
        with mock.patch("XianyuAutoAsync.db_manager.get_cookie", return_value=STORED):
            self.assertTrue(XianyuLive.authorize_chat_handoff("handoff-acc", REORDERED))
        self.assertIsNotNone(XianyuLive.consume_chat_handoff("handoff-acc", REORDERED))

    def test_different_cookie2_is_rejected(self):
        with mock.patch("XianyuAutoAsync.db_manager.get_cookie", return_value=STORED):
            self.assertFalse(XianyuLive.authorize_chat_handoff("handoff-acc", OTHER_COOKIE2))
        self.assertIsNone(XianyuLive.consume_chat_handoff("handoff-acc", OTHER_COOKIE2))

    def test_different_unb_is_rejected(self):
        with mock.patch("XianyuAutoAsync.db_manager.get_cookie", return_value=STORED):
            self.assertFalse(XianyuLive.authorize_chat_handoff("handoff-acc", OTHER_UNB))

    def test_incomplete_cookie_falls_back_to_exact_match(self):
        with mock.patch("XianyuAutoAsync.db_manager.get_cookie", return_value=INCOMPLETE):
            self.assertTrue(XianyuLive.authorize_chat_handoff("handoff-acc", INCOMPLETE))
        self.assertIsNotNone(XianyuLive.consume_chat_handoff("handoff-acc", INCOMPLETE))
        with mock.patch("XianyuAutoAsync.db_manager.get_cookie", return_value=INCOMPLETE):
            self.assertFalse(XianyuLive.authorize_chat_handoff("handoff-acc", INCOMPLETE_REORDERED))


class ChatFlagIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="login_self_heal_")
        self.store = DBManager(db_path=os.path.join(self.tmpdir, "heal.db"))

    def tearDown(self):
        try:
            self.store.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reordered_cookie_clears_flag_and_other_cookie_does_not(self):
        self.assertTrue(self.store.save_cookie("heal-acc", STORED))
        self.assertTrue(self.store.set_chat_verification_required("heal-acc", True, expected_cookie=STORED))
        self.assertTrue(self.store.set_chat_verification_required("heal-acc", False, expected_cookie=REORDERED))
        self.assertFalse(self.store.get_chat_verification_required("heal-acc"))

        self.assertTrue(self.store.set_chat_verification_required("heal-acc", True, expected_cookie=STORED))
        self.assertFalse(self.store.set_chat_verification_required("heal-acc", False, expected_cookie=OTHER_COOKIE2))
        self.assertTrue(self.store.get_chat_verification_required("heal-acc"))
        self.assertFalse(self.store.set_chat_verification_required("heal-acc", True, expected_cookie=OTHER_COOKIE2))
        self.assertTrue(self.store.get_chat_verification_required("heal-acc"))

    def test_grace_recheck_failures_reset_when_new_scan_starts(self):
        self.assertTrue(self.store.save_cookie("heal-acc", STORED))
        self.assertEqual(
            self.store.record_token_recovery_grace_recheck_failure("heal-acc", "h5_token_expired"),
            1,
        )
        self.assertEqual(
            self.store.record_token_recovery_grace_recheck_failure("heal-acc", "h5_token_expired"),
            2,
        )
        budget = self.store.get_token_recovery_budget("heal-acc")
        self.assertEqual(budget["grace_recheck_failures"], 2)
        self.assertEqual(budget["grace_recheck_reason"], "h5_token_expired")
        self.assertEqual(budget["grace_rechecked"], 0)
        self.assertTrue(self.store.set_cookie_qr_login_grace_until("heal-acc", int(time.time()) + 600))
        budget = self.store.get_token_recovery_budget("heal-acc")
        self.assertEqual(budget["grace_recheck_failures"], 0)
        self.assertEqual(budget["grace_recheck_reason"], "")
        self.assertEqual(budget["grace_rechecked"], 0)


class QrPrewarmTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._auth_prewarmed_tokens.pop("prewarm-acc", None)

    def test_valid_probe_caches_token(self):
        cookie = STORED
        probe = {
            "status": "cookie_valid",
            "verification_url": None,
            "payload": {"ret": ["SUCCESS::调用成功"], "data": {"accessToken": "token-ok"}},
        }

        async def run():
            with mock.patch(
                "utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie",
                return_value=probe,
            ) as probed:
                result = await XianyuLive.prewarm_token_from_cookie("prewarm-acc", cookie, source="qr_login")
            return result, probed

        result, probed = asyncio.run(run())
        self.assertTrue(result["cached"])
        self.assertFalse(result["platform_punish"])
        self.assertIn("prewarm-acc", XianyuLive._auth_prewarmed_tokens)
        self.assertEqual(XianyuLive._auth_prewarmed_tokens["prewarm-acc"]["token"], "token-ok")
        probed.assert_called_once()

    def test_punish_probe_does_not_cache_token(self):
        probe = {
            "status": "verification_required",
            "verification_url": "https://example.invalid/_____tmd_____/punish?x5secdata=1",
            "payload": {
                "ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::哎哟喂,被挤爆啦"],
                "data": {"url": "https://example.invalid/_____tmd_____/punish?x5secdata=1"},
            },
        }

        async def run():
            with mock.patch(
                "utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie",
                return_value=probe,
            ):
                return await XianyuLive.prewarm_token_from_cookie("prewarm-acc", STORED, source="qr_login")

        result = asyncio.run(run())
        self.assertFalse(result["cached"])
        self.assertTrue(result["platform_punish"])
        self.assertIsNone(result["token"])
        self.assertNotIn("prewarm-acc", XianyuLive._auth_prewarmed_tokens)


class GraceRecheckTests(unittest.TestCase):
    def tearDown(self):
        XianyuLive._auth_prewarmed_tokens.pop("grace-acc", None)

    def test_plain_token_expiry_is_not_account_punish(self):
        self.assertFalse(XianyuLive.response_is_platform_punish("FAIL_SYS_TOKEN_EXOIRED::令牌过期"))
        self.assertFalse(XianyuLive.response_is_platform_punish({"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"]}))
        self.assertTrue(XianyuLive.response_is_h5_token_expired("FAIL_SYS_TOKEN_EXOIRED::令牌过期"))
        self.assertTrue(XianyuLive.response_is_platform_punish("RGV587_ERROR"))

    def _grace_live(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = "grace-acc"
        live.cookies_str = STORED
        live.proxy_config = None
        live.chat_verification_required = True
        live._get_qr_login_grace_remaining_seconds = lambda: 0
        live.send_token_refresh_notification = mock.AsyncMock()
        return live

    def test_stuck_recheck_flag_still_retries_then_second_success_clears(self):
        live = self._grace_live()
        budget = {
            "attempts": 0,
            "hold_until": 0,
            "hold_reason": "",
            "grace_rechecked": 1,
            "grace_recheck_failures": 0,
            "grace_recheck_reason": "",
        }
        chat_flag = {"value": True}
        marked = {"done": False}
        probes = [
            {
                "cached": False,
                "status": "unknown",
                "platform_punish": False,
                "h5_token_expired": True,
                "h5_token_refreshed": False,
                "cookies_str": STORED,
                "token": None,
            },
            {
                "cached": True,
                "status": "cookie_valid",
                "platform_punish": False,
                "h5_token_expired": False,
                "h5_token_refreshed": False,
                "cookies_str": STORED,
                "token": "access-1",
            },
        ]

        def get_flag(_cookie_id):
            return chat_flag["value"]

        def set_flag(_cookie_id, required, expected_cookie=None):
            if expected_cookie != live.cookies_str:
                return False
            chat_flag["value"] = bool(required)
            return True

        def get_budget(_cookie_id):
            return dict(budget)

        def record_failure(_cookie_id, reason=""):
            budget["grace_recheck_failures"] += 1
            budget["grace_recheck_reason"] = reason
            return budget["grace_recheck_failures"]

        def clear_budget(_cookie_id):
            budget["attempts"] = 0
            budget["hold_until"] = 0
            budget["hold_reason"] = ""
            budget["grace_recheck_failures"] = 0
            return True

        def mark(_cookie_id):
            marked["done"] = True
            budget["grace_rechecked"] = 1
            return True

        async def prewarm(*_args, **_kwargs):
            return probes.pop(0)

        async def run():
            await live._maybe_recheck_token_after_qr_grace()
            self.assertTrue(chat_flag["value"])
            self.assertEqual(budget["grace_recheck_failures"], 1)
            self.assertEqual(budget["grace_recheck_reason"], "h5_token_expired")
            self.assertFalse(marked["done"])
            await live._maybe_recheck_token_after_qr_grace()

        with mock.patch("XianyuAutoAsync.db_manager.get_chat_verification_required", get_flag), \
                mock.patch("XianyuAutoAsync.db_manager.set_chat_verification_required", set_flag), \
                mock.patch("XianyuAutoAsync.db_manager.get_token_recovery_budget", get_budget), \
                mock.patch("XianyuAutoAsync.db_manager.record_token_recovery_grace_recheck_failure", record_failure), \
                mock.patch("XianyuAutoAsync.db_manager.clear_token_recovery_budget", clear_budget), \
                mock.patch("XianyuAutoAsync.db_manager.mark_token_recovery_grace_rechecked", mark), \
                mock.patch.object(XianyuLive, "prewarm_token_from_cookie", prewarm):
            asyncio.run(run())

        self.assertFalse(chat_flag["value"])
        self.assertFalse(live.chat_verification_required)
        self.assertEqual(live.current_token, "access-1")
        self.assertEqual(live.last_token_refresh_status, "success")
        self.assertTrue(marked["done"])
        self.assertNotIn("grace-acc", XianyuLive._auth_prewarmed_tokens)

    def test_recheck_stops_after_failure_cap(self):
        live = self._grace_live()
        budget = {
            "attempts": 0,
            "hold_until": 0,
            "hold_reason": "",
            "grace_rechecked": 0,
            "grace_recheck_failures": 3,
            "grace_recheck_reason": "h5_token_expired",
        }

        async def prewarm(*_args, **_kwargs):
            raise AssertionError("已达失败上限，不应再预检")

        async def run():
            await live._maybe_recheck_token_after_qr_grace()

        with mock.patch("XianyuAutoAsync.db_manager.get_chat_verification_required", return_value=True), \
                mock.patch("XianyuAutoAsync.db_manager.get_token_recovery_budget", return_value=budget), \
                mock.patch.object(XianyuLive, "prewarm_token_from_cookie", prewarm):
            asyncio.run(run())
        self.assertTrue(live.chat_verification_required)


class HealthGradeTests(unittest.TestCase):
    def test_connected_without_token_is_degraded_not_offline(self):
        grade = reply_server.classify_account_health({
            "connection_state": "connected",
            "ws_ready": True,
            "has_current_token": False,
            "token_refresh_status": "token_expired_recovery_failed",
            "last_heartbeat_age_seconds": 6,
        })
        self.assertEqual(grade["health_grade"], "B")
        self.assertFalse(grade["alert_offline"])
        self.assertNotIn("账号离线", grade["health_summary"])
        self.assertNotIn("无法登录", grade["health_summary"])
        self.assertNotIn("买家收不到", grade["health_summary"])
        self.assertIn("扫码续期", grade["health_summary"])

    def test_connected_with_token_is_online(self):
        grade = reply_server.classify_account_health({
            "connection_state": "connected",
            "ws_ready": True,
            "has_current_token": True,
            "token_refresh_status": "success",
            "last_heartbeat_age_seconds": 6,
        })
        self.assertEqual(grade["health_grade"], "C")
        self.assertEqual(grade["health_summary"], "账号在线")
        self.assertFalse(grade["alert_offline"])

    def test_disconnected_is_offline(self):
        grade = reply_server.classify_account_health({
            "connection_state": "reconnecting",
            "ws_ready": False,
            "has_current_token": False,
            "token_refresh_status": None,
            "last_heartbeat_age_seconds": None,
        })
        self.assertEqual(grade["health_grade"], "A")
        self.assertTrue(grade["alert_offline"])
        self.assertIn("账号离线", grade["health_summary"])

    def test_platform_hold_stays_degraded_even_when_connection_is_down(self):
        status = {
            "connection_state": "reconnecting",
            "ws_ready": False,
            "has_current_token": False,
            "token_refresh_status": "chat_verification_required",
            "token_refresh_error_message": "聊天需要完成平台验证，请扫码或更新有效凭证",
            "last_heartbeat_age_seconds": None,
        }
        budget = {
            "attempts": 5,
            "hold_until": int(time.time()) + 3600,
            "hold_reason": "platform_punish",
            "grace_rechecked": 0,
        }
        with mock.patch.object(reply_server.db_manager, "get_token_recovery_budget", return_value=budget):
            graded = reply_server._apply_token_recovery_hold_status(dict(status), "2222369601860")
        self.assertEqual(graded["health_grade"], "B")
        self.assertFalse(graded["alert_offline"])
        self.assertEqual(graded["token_refresh_status"], "platform_risk_hold")
        self.assertIn("重复扫码无效", graded["health_summary"])
        self.assertNotIn("账号离线", graded["health_summary"])
        self.assertNotIn("无法登录", graded["health_summary"])
        self.assertNotIn("买家收不到", graded["health_summary"])
