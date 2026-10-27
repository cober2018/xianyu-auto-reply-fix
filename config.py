import os
import yaml
from typing import Dict, Any

class Config:
    """配置管理类
    
    用于加载和管理全局配置文件(global_config.yml)。
    支持配置的读取、修改和保存。
    """
    
    _instance = None
    _config = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """加载配置文件
        
        从global_config.yml文件中加载配置信息。
        如果文件不存在则抛出FileNotFoundError异常。
        """
        config_path = os.path.join(os.path.dirname(__file__), 'global_config.yml')
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项
        
        Args:
            key: 配置项的键，支持点号分隔的多级键
            default: 当配置项不存在时返回的默认值
            
        Returns:
            配置项的值或默认值
        """
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """设置配置项
        
        Args:
            key: 配置项的键，支持点号分隔的多级键
            value: 要设置的值
        """
        keys = key.split('.')
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def save(self) -> None:
        """保存配置到文件
        
        将当前配置保存回global_config.yml文件
        """
        config_path = os.path.join(os.path.dirname(__file__), 'global_config.yml')
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(self._config, f, allow_unicode=True, default_flow_style=False)

    @property
    def config(self) -> Dict[str, Any]:
        """获取完整配置
        
        Returns:
            包含所有配置项的字典
        """
        return self._config

# 创建全局配置实例
config = Config()

# 导出常用配置项
COOKIES_STR = config.get('COOKIES.value', '')
COOKIES_LAST_UPDATE = config.get('COOKIES.last_update_time', '')
WEBSOCKET_URL = config.get('WEBSOCKET_URL', 'wss://wss-goofish.dingtalk.com/')
HEARTBEAT_INTERVAL = config.get('HEARTBEAT_INTERVAL', 15)
HEARTBEAT_TIMEOUT = config.get('HEARTBEAT_TIMEOUT', 30)
TOKEN_REFRESH_INTERVAL = config.get('TOKEN_REFRESH_INTERVAL', 72000)
TOKEN_RETRY_INTERVAL = config.get('TOKEN_RETRY_INTERVAL', 7200)
SESSION_KEEPALIVE_INTERVAL = config.get('SESSION_KEEPALIVE_INTERVAL', 600)
SESSION_KEEPALIVE_RETRY_INTERVAL = config.get('SESSION_KEEPALIVE_RETRY_INTERVAL', 180)
MESSAGE_EXPIRE_TIME = config.get('MESSAGE_EXPIRE_TIME', 300000)
SLIDER_VERIFICATION = config.get('SLIDER_VERIFICATION', {
    'max_concurrent': 3,
    'wait_timeout': 60
})
API_ENDPOINTS = config.get('API_ENDPOINTS', {})
DEFAULT_HEADERS = config.get('DEFAULT_HEADERS', {})
WEBSOCKET_HEADERS = config.get('WEBSOCKET_HEADERS', {})
APP_CONFIG = config.get('APP_CONFIG', {})
AUTO_REPLY = config.get('AUTO_REPLY', {
    'enabled': True,
    'default_message': '亲爱的"{send_user_name}" 老板你好！所有宝贝都可以拍，秒发货的哈~不满意的话可以直接申请退款哈~',
    'api': {
        'enabled': False,
        'url': 'http://localhost:8090/xianyu/reply',
        'timeout': 10
    }
})
MANUAL_MODE = config.get('MANUAL_MODE', {})
LOG_CONFIG = config.get('LOG_CONFIG', {})
YIFAN_API = config.get('YIFAN_API', {
    'callback_url': 'http://116.196.116.76/yifan.php',
    'query_url': 'http://116.196.116.76/yifan.php'
})
RISK_CONTROL = config.get('RISK_CONTROL', {
    'night_mode_enabled': True,
    'night_start_hour': 0,
    'night_end_hour': 7,
    'qr_login_grace_minutes': 15,
    'night_keepalive_multiplier': 3,
    'night_cookie_refresh_multiplier': 2,
    'backoff_escalation_factor': 1.5,
    'backoff_max_cap_seconds': 3600,
    'consecutive_failure_protection_threshold': 5,
    'post_slider_retry_delay_min': 5.0,
    'post_slider_retry_delay_max': 10.0,
    'token_refresh_dedup_window_seconds': 60,
    'token_retry_min_wait_seconds': 180,
    # Token 预检连续失败：10分钟 → 30分钟 → 2小时，第5次或账号级风控后停止打接口
    'token_recovery_backoff_schedule_seconds': [600, 1800, 7200],
    'token_recovery_max_attempts': 5,
    'token_recovery_hold_seconds': 21600,
    # 扫码后没有预热到 Token 时，首轮预检前的落库稳定等待
    'token_preflight_settle_seconds': 5,
    # 扫码稳定期结束后，预检失败最多再试这么多次；成功才记完成
    'token_recovery_grace_recheck_max_attempts': 3,
    'max_post_slider_session_retries': 1,
    'soft_auth_token_preflight_enabled': True,
    'soft_auth_token_preflight_timeout_seconds': 5.0,
    'soft_auth_token_preflight_qr_enabled': False,
    # 拟人化：付款到自动发货之间的随机延迟区间(秒)。秒发是机器人签名，真人卖家需要时间处理订单
    'auto_delivery_delay_range': [20, 90],
    # 拟人化：夜间窗口内发货延迟拉长(秒)，真人卖家凌晨处理订单更慢；不宜过长以免影响买家体验
    'auto_delivery_night_delay_range': [180, 900],
    # 普通客服夜间保持秒级响应，显式运营配置仍可覆盖
    'night_reply_delay_range': [2, 10],
    'auto_captcha_recovery_enabled': False,
    # 履约一致性巡检：平台 shipped 但本地无 finalized 的扫描间隔（秒）
    'delivery_gap_scan_interval': 300,
    # 巡检命中后是否自动尝试补写发货凭证（仅在已有已送达内容时，绝不新发卡密）
    'delivery_gap_auto_retry': True,
    # 待发货订单滞留多少分钟后告警运营方
    'delivery_gap_pending_ship_alert_minutes': 15,
    # 发货失败（私聊回执超时等）是否主动告警运营方
    'delivery_failure_alert_enabled': True,
})
# 发货凭证直填（tradeText）链路：与私聊 WebSocket 解耦的主发货通道
DELIVERY = config.get('DELIVERY', {
    # tradeText 发货凭证直填总开关（灰度用；false 时行为与旧私聊链路完全一致）
    'trade_text_enabled': True,
    # 发货凭证长度上限（字符）
    'trade_text_max_length': 500,
    # 凭证失败时是否回落旧私聊链路（true = 不倒退）
    'trade_text_fallback_to_chat': True,
    # 私聊补充告知失败是否忽略（true = 不影响履约；缺口仍会记账并告警）
    'chat_notify_best_effort': True,
    # 双通道改造：私信缺口自动补发（凭证已送达+私信未送达时，用原样快照补发私信）
    'chat_gap_auto_resend_enabled': True,
    # 私信缺口自动补发最大次数（达到后停止重试，转人工告警）
    'chat_gap_resend_max_attempts': 3,
    # 首次发现私信缺口到首次自动补发的宽限秒数（默认900=15分钟，覆盖扫码稳定期并留人工反应窗口）
    'chat_gap_resend_grace_seconds': 900,
    # 私信补发失败的退避基准秒数（指数翻倍，上限取 RISK_CONTROL.backoff_max_cap_seconds）
    'chat_gap_resend_backoff_base_seconds': 1800,
    # 私信缺口扫描时间窗（小时），只扫窗口内的订单
    'chat_gap_scan_window_hours': 72,
    # 掉线恢复在线后一次性补拉掉线期间订单（防掉线失明）
    'orders_recovery_sync_enabled': True,
    # 掉线恢复补拉的最小间隔秒数（防重连风暴反复拉单）
    'orders_recovery_sync_min_interval_seconds': 1800,
    # 掉线恢复补拉的延迟秒数（等连接稳定）
    'orders_recovery_sync_delay_seconds': 60,
})
_cookies_raw = config.get('COOKIES', [])
if isinstance(_cookies_raw, list):
    COOKIES_LIST = _cookies_raw
else:
    # 兼容旧格式，仅有 value 字段
    val = _cookies_raw.get('value') if isinstance(_cookies_raw, dict) else None
    COOKIES_LIST = [{'id': 'default', 'value': val}] if val else []
