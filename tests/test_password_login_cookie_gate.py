import asyncio
import unittest
from unittest.mock import patch

from XianyuAutoAsync import (
    REQUIRED_SESSION_COOKIE_FIELDS,
    XianyuLive,
)
from utils.xianyu_slider_stealth import XianyuSliderStealth


def _build_cookie_dict(overrides=None, drop=()):
    """构造一份核心字段齐全的密码登录 Cookie 快照。"""
    fields = {
        'unb': '2222369601860',
        'sgcookie': 'sg' * 20,
        'cookie2': 'c2' * 20,
        '_m_h5_tk': 'tk' * 16,
        '_m_h5_tk_enc': 'enc' * 16,
        't': 't-token',
        'cna': 'cna-value',
        '_tb_token_': 'tb-token',
    }
    fields.update(overrides or {})
    for key in drop:
        fields.pop(key, None)
    return fields


class PasswordLoginCookieGateTest(unittest.TestCase):
    """回归：密码登录成功但 Cookie 残缺/换不到 token 时，必须拒绝落库。

    现场(2026-10-09 生产)：32 字段 Cookie 缺 _m_h5_tk/_m_h5_tk_enc 落库，
    实例热重启循环每 59 秒完整登录一次，持续累积风控。
    """

    def test_havana_not_in_protected_fields(self):
        # havana_lgc2_77 已不再下发，留在保护清单会把成功登录误判失败(Bug#1)
        self.assertNotIn('havana_lgc2_77', XianyuSliderStealth._PROTECTED_SESSION_COOKIE_FIELDS)
        # 硬门槛字段保持完整：token 三件套缺一不可
        for field in ('_m_h5_tk', '_m_h5_tk_enc', 'unb'):
            self.assertIn(field, XianyuSliderStealth._REQUIRED_SESSION_COOKIE_FIELDS)
            self.assertIn(field, REQUIRED_SESSION_COOKIE_FIELDS)

    def test_gate_blocks_cookie_missing_token_fields(self):
        # 闸门第一层：结构校验(与 _try_password_login_refresh 内联逻辑一致)
        incomplete = _build_cookie_dict(drop=('_m_h5_tk', '_m_h5_tk_enc'))
        missing = [
            key for key in REQUIRED_SESSION_COOKIE_FIELDS
            if not str(incomplete.get(key) or '').strip()
        ]
        self.assertEqual(sorted(missing), ['_m_h5_tk', '_m_h5_tk_enc'])

        complete = _build_cookie_dict()
        missing = [
            key for key in REQUIRED_SESSION_COOKIE_FIELDS
            if not str(complete.get(key) or '').strip()
        ]
        self.assertEqual(missing, [])

    def test_token_preflight_only_accepts_definitive_valid(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'test-account'
        live.proxy_config = {}
        cookie_str = '; '.join(f'{k}={v}' for k, v in _build_cookie_dict().items())

        # 明确拿到 token 载荷 → 放行
        with patch('utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie',
                   return_value={'status': 'cookie_valid'}):
            ok, reason = asyncio.run(live._verify_new_cookie_token_usable(cookie_str))
        self.assertTrue(ok)
        self.assertEqual(reason, '')

        # 触发验证 → 拦截(不重试)
        with patch('utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie',
                   return_value={'status': 'verification_required'}):
            ok, reason = asyncio.run(live._verify_new_cookie_token_usable(cookie_str))
        self.assertFalse(ok)
        self.assertIn('verification_required', reason)

        # 结构性缺失(_m_h5_tk 不存在) → 立即拦截
        with patch('utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie',
                   side_effect=ValueError('Cookie 缺少 unb 或 _m_h5_tk')):
            ok, reason = asyncio.run(live._verify_new_cookie_token_usable(cookie_str))
        self.assertFalse(ok)
        self.assertIn('_m_h5_tk', reason)

    def test_token_preflight_blocks_persistent_unknown(self):
        # 错域 token 在该接口的表现是 unknown；连续 unknown 必须拦截
        live = XianyuLive.__new__(XianyuLive)
        live.cookie_id = 'test-account'
        live.proxy_config = {}
        cookie_str = '; '.join(f'{k}={v}' for k, v in _build_cookie_dict().items())

        async def _noop_sleep(_delay, *args, **kwargs):
            return None

        with patch('utils.xianyu_slider_stealth.probe_cookie_verification_from_cookie',
                   return_value={'status': 'unknown'}), \
             patch.object(asyncio, 'sleep', _noop_sleep):
            ok, reason = asyncio.run(live._verify_new_cookie_token_usable(cookie_str))
        self.assertFalse(ok)
        self.assertIn('unknown', reason)

    def test_classify_cookie_incomplete_not_counted_for_pause(self):
        reason, seconds = XianyuLive.classify_password_login_failure(
            '密码登录Cookie缺少核心字段: _m_h5_tk, _m_h5_tk_enc'
        )
        self.assertEqual(reason, 'cookie_incomplete')
        self.assertEqual(seconds, 300)
        # 不计入连续失败停号(只有 slider_failed/risk_control 计数)
        self.assertFalse(XianyuLive._is_counted_password_login_failure_reason('cookie_incomplete'))


if __name__ == '__main__':
    unittest.main()
