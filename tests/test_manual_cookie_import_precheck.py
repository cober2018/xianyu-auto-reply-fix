import unittest
from unittest import mock

import utils.xianyu_slider_stealth as slider_module


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeCookies:
    def __init__(self):
        self.values = {}

    def update(self, data):
        self.values.update(data)

    def get_dict(self):
        return dict(self.values)


class _FakeSession:
    def __init__(self, payload, response_cookies=None):
        self.headers = {}
        self.cookies = _FakeCookies()
        self._payload = payload
        self._response_cookies = dict(response_cookies or {})
        self.post_calls = []

    def post(self, url, params=None, data=None, proxies=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "params": params,
                "data": data,
                "proxies": proxies,
                "timeout": timeout,
            }
        )
        self.cookies.update(self._response_cookies)
        return _FakeResponse(self._payload)


class ManualCookieImportPrecheckTest(unittest.TestCase):
    def _run_probe(self, payload, response_cookies=None):
        fake_session = _FakeSession(payload, response_cookies=response_cookies)
        with mock.patch("requests.Session", return_value=fake_session):
            result = slider_module.probe_cookie_verification_from_cookie(
                "unb=test_user; _m_h5_tk=test_token_12345; cookie2=dummy",
                proxy=None,
            )
        return result, fake_session

    def test_probe_cookie_verification_returns_verification_url_when_present(self):
        result, fake_session = self._run_probe(
            {
                "ret": ["FAIL_SYS_USER_VALIDATE::校验失败"],
                "data": {"url": "https://passport.goofish.com/identity_verify"},
                "v": "1.0",
            }
        )

        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(
            result["verification_url"],
            "https://passport.goofish.com/identity_verify",
        )
        self.assertEqual(result["session_cookies"]["unb"], "test_user")
        self.assertEqual(fake_session.post_calls[0]["timeout"], 30)
        self.assertEqual(len(fake_session.post_calls), 1)
        self.assertFalse(result["h5_token_refreshed"])

    def test_probe_cookie_verification_treats_success_token_payload_as_cookie_valid(self):
        result, _fake_session = self._run_probe(
            {
                "ret": ["SUCCESS::调用成功"],
                "data": {
                    "accessToken": "oauth_access_token",
                    "refreshToken": "oauth_refresh_token",
                },
                "v": "1.0",
            },
            response_cookies={
                "_m_h5_tk": "refreshed_token_12345",
                "cookie2": "updated_cookie2",
            },
        )

        self.assertEqual(result["status"], "cookie_valid")
        self.assertIsNone(result["verification_url"])
        self.assertTrue(result["payload"]["data"]["accessToken"])
        self.assertEqual(
            result["session_cookies"]["_m_h5_tk"],
            "refreshed_token_12345",
        )
        self.assertEqual(
            result["session_cookies"]["cookie2"],
            "updated_cookie2",
        )
        self.assertEqual(len(_fake_session.post_calls), 1)
        self.assertFalse(result["h5_token_refreshed"])

    def test_expired_h5_token_is_refreshed_then_classified_as_valid(self):
        fake_session = _SequencedSession(
            [
                {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}},
                {
                    "ret": ["SUCCESS::调用成功"],
                    "data": {"accessToken": "oauth_access_token"},
                },
            ],
            [
                {"_m_h5_tk": "newtoken456_222", "_m_h5_tk_enc": "enc-new"},
                {},
            ],
        )
        cookie = "unb=test_user; _m_h5_tk=oldtoken123_111; cookie2=dummy"
        with mock.patch("requests.Session", return_value=fake_session):
            result = slider_module.probe_cookie_verification_from_cookie(cookie, proxy=None)

        self.assertEqual(result["status"], "cookie_valid")
        self.assertEqual(len(fake_session.post_calls), 2)
        self.assertTrue(result["h5_token_refreshed"])
        self.assertFalse(result["h5_token_expired"])
        second = fake_session.post_calls[1]
        expected = slider_module.build_cookie_verification_sign(
            second["params"]["t"],
            "newtoken456",
            second["data"]["data"],
        )
        self.assertEqual(second["params"]["sign"], expected)
        self.assertIn("_m_h5_tk=newtoken456_222", result["cookie_text"])
        self.assertNotIn("oldtoken123_111", result["cookie_text"])

    def test_expired_h5_then_punish_is_not_reported_as_token_expired(self):
        punish_url = "https://example.invalid/_____tmd_____/punish?x5secdata=1"
        fake_session = _SequencedSession(
            [
                {"ret": ["FAIL_SYS_TOKEN_EXPIRED::令牌过期"], "data": {}},
                {
                    "ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦"],
                    "data": {"url": punish_url},
                },
            ],
            [{"_m_h5_tk": "newtoken456_222"}, {}],
        )
        cookie = "unb=test_user; _m_h5_tk=oldtoken123_111; cookie2=dummy"
        with mock.patch("requests.Session", return_value=fake_session):
            result = slider_module.probe_cookie_verification_from_cookie(cookie, proxy=None)

        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(result["verification_url"], punish_url)
        self.assertEqual(len(fake_session.post_calls), 2)
        self.assertTrue(result["h5_token_refreshed"])
        self.assertFalse(result["h5_token_expired"])
        from XianyuAutoAsync import XianyuLive
        self.assertTrue(XianyuLive.response_is_platform_punish(result["payload"]))
        self.assertFalse(XianyuLive.response_is_platform_punish("FAIL_SYS_TOKEN_EXOIRED::令牌过期"))

    def test_expired_h5_without_new_token_stays_unknown(self):
        result, fake_session = self._run_probe(
            {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"], "data": {}},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(fake_session.post_calls), 1)
        self.assertTrue(result["h5_token_expired"])
        self.assertFalse(result["h5_token_refreshed"])
        from XianyuAutoAsync import XianyuLive
        self.assertFalse(XianyuLive.response_is_platform_punish(result["payload"]))


class _SequencedSession(_FakeSession):
    def __init__(self, payloads, response_cookie_sequence):
        super().__init__(payloads[0] if payloads else {})
        self._payloads = list(payloads)
        self._cookie_sequence = list(response_cookie_sequence)
        self._index = 0

    def post(self, url, params=None, data=None, proxies=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "params": params,
                "data": data,
                "proxies": proxies,
                "timeout": timeout,
            }
        )
        index = min(self._index, len(self._payloads) - 1)
        cookie_index = min(self._index, len(self._cookie_sequence) - 1)
        self._index += 1
        self.cookies.update(self._cookie_sequence[cookie_index])
        return _FakeResponse(self._payloads[index])


if __name__ == "__main__":
    unittest.main()
