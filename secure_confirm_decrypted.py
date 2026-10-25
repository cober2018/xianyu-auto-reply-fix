"""
自动确认发货模块 - 解密版本
这是secure_confirm_ultra.py的解密版本，用于自动确认发货功能
"""

import asyncio
import json
import time
import aiohttp
from loguru import logger
from utils.xianyu_utils import generate_sign, trans_cookies, get_consistent_browser_identity

# 发货凭证（tradeText）长度硬上限；可被 auto_confirm(trade_text_max_length=...) 覆盖用于灰度压测
MAX_TRADE_TEXT_LENGTH = 500


class SecureConfirm:
    """自动确认发货类"""

    def __init__(self, session, cookies_str, cookie_id, main_instance=None):
        """
        初始化确认发货实例

        Args:
            session: aiohttp会话对象
            cookies_str: Cookie字符串
            cookie_id: Cookie ID
            main_instance: 主实例对象（XianyuLive）
        """
        self.session = session
        self.cookies_str = cookies_str
        self.cookie_id = cookie_id
        self.main_instance = main_instance

        # 解析cookies
        self.cookies = trans_cookies(cookies_str) if cookies_str else {}

        # Token相关属性
        self.current_token = None
        self.last_token_refresh_time = 0
        self.token_refresh_interval = 3600  # 1小时

    def _serialize_cookies(self, cookies_dict=None):
        cookies = cookies_dict or self.cookies
        return '; '.join([f"{k}={v}" for k, v in cookies.items() if k])

    def _set_runtime_cookie_state(self, cookies_dict=None, cookies_str=None):
        normalized_cookies = dict(cookies_dict or trans_cookies(cookies_str or ""))
        if not normalized_cookies:
            return False

        previous_cookie_string = self.cookies_str
        self.cookies = normalized_cookies
        self.cookies_str = self._serialize_cookies(normalized_cookies)
        return self.cookies_str != previous_cookie_string

    def _extract_set_cookie_updates(self, response_headers):
        if not response_headers:
            return {}

        set_cookie_values = []
        try:
            if hasattr(response_headers, 'getall') and 'set-cookie' in response_headers:
                set_cookie_values = response_headers.getall('set-cookie', [])
            elif hasattr(response_headers, 'get_all'):
                set_cookie_values = response_headers.get_all('set-cookie', [])
            elif isinstance(response_headers, dict):
                raw_value = response_headers.get('set-cookie') or response_headers.get('Set-Cookie')
                if isinstance(raw_value, list):
                    set_cookie_values = raw_value
                elif raw_value:
                    set_cookie_values = [raw_value]
        except Exception:
            set_cookie_values = []

        updates = {}
        for cookie in set_cookie_values:
            if '=' not in cookie:
                continue
            name, value = cookie.split(';')[0].split('=', 1)
            updates[name.strip()] = value.strip()
        return updates

    async def _apply_response_cookie_updates(self, response_headers):
        updates = self._extract_set_cookie_updates(response_headers)
        if not updates:
            return False

        merged_cookies = dict(self.cookies)
        merged_cookies.update(updates)
        changed = self._set_runtime_cookie_state(cookies_dict=merged_cookies)
        if changed:
            await self._update_config_cookies()
        return changed

    def _safe_str(self, obj):
        """安全字符串转换"""
        try:
            return str(obj)
        except:
            return "无法转换的对象"

    async def _get_real_item_id(self):
        """从数据库中获取一个真实的商品ID"""
        try:
            from db_manager import db_manager
            
            # 获取该账号的商品列表
            items = db_manager.get_items_by_cookie(self.cookie_id)
            if items:
                # 返回第一个商品的ID
                item_id = items[0].get('item_id')
                if item_id:
                    logger.debug(f"【{self.cookie_id}】获取到真实商品ID: {item_id}")
                    return item_id
            
            # 如果该账号没有商品，尝试获取任意一个商品ID
            all_items = db_manager.get_all_items()
            if all_items:
                item_id = all_items[0].get('item_id')
                if item_id:
                    logger.debug(f"【{self.cookie_id}】使用其他账号的商品ID: {item_id}")
                    return item_id
            
            logger.warning(f"【{self.cookie_id}】数据库中没有找到任何商品ID")
            return None
            
        except Exception as e:
            logger.error(f"【{self.cookie_id}】获取真实商品ID失败: {self._safe_str(e)}")
            return None

    async def _update_config_cookies(self):
        """更新数据库中的Cookie配置"""
        try:
            from db_manager import db_manager
            # 更新数据库中的cookies
            db_manager.update_cookie_account_info(self.cookie_id, cookie_value=self.cookies_str)
            logger.debug(f"【{self.cookie_id}】已更新数据库中的Cookie")
        except Exception as e:
            logger.error(f"【{self.cookie_id}】更新数据库Cookie失败: {self._safe_str(e)}")


    async def auto_confirm(self, order_id, item_id=None, retry_count=0, trade_text: str = '',
                           trade_text_max_length: int = None):
        """自动确认发货 - 使用真实商品ID刷新token

        Args:
            trade_text: 发货凭证正文（写入订单详情页的发货留言，网盘链接+提取码）。
                为空时 data_val 与旧版逐字节一致，行为完全不变。
            trade_text_max_length: 凭证正文长度上限；None 时用模块默认 500。
                P0 灰度验证超长场景时可显式放大。
        """
        if retry_count >= 4:  # 最多重试3次
            logger.error("自动确认发货失败，重试次数过多")
            return {"error": "自动确认发货失败，重试次数过多"}

        try:
            max_trade_len = int(trade_text_max_length) if trade_text_max_length else MAX_TRADE_TEXT_LENGTH
        except (TypeError, ValueError):
            max_trade_len = MAX_TRADE_TEXT_LENGTH
        safe_trade_text = str(trade_text or '').strip()[:max(0, max_trade_len)]

        # 保存item_id供Token刷新使用
        if item_id:
            self._current_item_id = item_id
            logger.debug(f"【{self.cookie_id}】设置当前商品ID: {item_id}")

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.logistic.consign.dummy',
            'sessionOption': 'AutoLoginOnly',
        }

        # 签名一致性红线：sign 必须由最终发送的 data_val 逐字节同源计算（下方 generate_sign）。
        # 紧凑 JSON 序列化；空凭证时与旧版仅差 orderId 后一个空格，JSON 语义等价且签名同源。
        data_val = json.dumps({
            "orderId": str(order_id or ''),
            "tradeText": safe_trade_text,
            "picList": [],
            "newUnconsign": True,
        }, ensure_ascii=False, separators=(',', ':'))
        if safe_trade_text:
            logger.info(f"【{self.cookie_id}】发货凭证直填模式: order_id={order_id}, 凭证长度={len(safe_trade_text)}")
        data = {
            'data': data_val,
        }

        # 始终从最新的cookies中获取_m_h5_tk token（刷新后cookies会被更新）
        token = trans_cookies(self.cookies_str).get('_m_h5_tk', '').split('_')[0] if trans_cookies(self.cookies_str).get('_m_h5_tk') else ''

        if token:
            logger.info(f"使用cookies中的_m_h5_tk token: {token}")
        else:
            logger.warning("cookies中没有找到_m_h5_tk token")

        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign

        try:
            logger.info(f"【{self.cookie_id}】开始自动确认发货，订单ID: {order_id}")

            # 设置请求超时
            request_timeout = aiohttp.ClientTimeout(total=30)
            # 避免跨事件循环复用session导致的timeout上下文错误；每次请求使用当前协程内的新session
            request_headers = {
                'cookie': self.cookies_str,
                'user-agent': get_consistent_browser_identity()['user_agent'],
                'accept': 'application/json',
                'content-type': 'application/x-www-form-urlencoded'
            }

            # 如主实例配置了HTTP代理，沿用代理
            proxy_url = None
            try:
                if self.main_instance is not None:
                    proxy_url = getattr(self.main_instance, '_http_proxy_url', None)
            except Exception:
                proxy_url = None

            async with aiohttp.ClientSession(headers=request_headers, timeout=request_timeout) as session:
                async with session.post(
                    'https://h5api.m.goofish.com/h5/mtop.taobao.idle.logistic.consign.dummy/1.0/',
                    params=params,
                    data=data,
                    proxy=proxy_url
                ) as response:
                    res_json = await response.json()

                    if await self._apply_response_cookie_updates(response.headers):
                        logger.debug("已更新Cookie到数据库")

                    logger.info(f"【{self.cookie_id}】自动确认发货响应: {res_json}")

                    # 检查响应结果
                    if res_json.get('ret') and res_json['ret'][0] == 'SUCCESS::调用成功':
                        logger.info(f"【{self.cookie_id}】✅ 自动确认发货成功，订单ID: {order_id}")
                        return {"success": True, "order_id": order_id}
                    else:
                        error_msg = res_json.get('ret', ['未知错误'])[0] if res_json.get('ret') else '未知错误'
                        logger.warning(f"【{self.cookie_id}】❌ 自动确认发货失败: {error_msg}")

                        if 'FAIL_SYS_SESSION_EXPIRED' in error_msg or 'Session过期' in error_msg:
                            return {
                                "error": error_msg,
                                "order_id": order_id,
                                "session_expired": True,
                                "need_relogin": True,
                                "confirm_retry_required": True,
                            }

                        if 'ORDER_STATUS_ERROR' in error_msg or '订单状态不正确' in error_msg:
                            return {
                                "error": error_msg,
                                "order_id": order_id,
                                "order_status_error": True,
                                "non_retryable": True,
                                "stop_confirm_retry": True,
                                "confirm_retry_required": False,
                            }

                        return await self.auto_confirm(order_id, item_id, retry_count + 1,
                                                       trade_text=trade_text,
                                                       trade_text_max_length=trade_text_max_length)


        except Exception as e:
            logger.error(f"【{self.cookie_id}】自动确认发货API请求异常: {self._safe_str(e)}")
            await asyncio.sleep(0.5)

            # 网络异常也进行重试
            if retry_count < 2:
                logger.info(f"【{self.cookie_id}】网络异常，准备重试...")
                return await self.auto_confirm(order_id, item_id, retry_count + 1,
                                               trade_text=trade_text,
                                               trade_text_max_length=trade_text_max_length)

            return {"error": f"网络异常: {self._safe_str(e)}", "order_id": order_id}
