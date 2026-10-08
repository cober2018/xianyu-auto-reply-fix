#!/usr/bin/env python3
"""上架节奏闸门:任何真实发布动作前必须先过这一关。

用法(OpenClaw 编排或脚本里,在调 POST /product-publish 之前):
    from pacing import wait_for_publish_slot
    wait_for_publish_slot()          # 距上次发布不足间隔时自动阻塞等待
    ... 调用发布接口 ...
    mark_published()                 # 发布成功后记录时间戳

规则:两次真实上架间隔 >= 5 分钟(环境变量 LISTING_MIN_INTERVAL_SECONDS 可调)。
状态文件: build/.last_publish_at (绝对时间戳, 秒)。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STATE_FILE = Path(__file__).parent / "build" / ".last_publish_at"
DEFAULT_MIN_INTERVAL = 300  # 秒。用户要求:上架间隔大于 5 分钟。


def _min_interval() -> float:
    try:
        return max(0.0, float(os.getenv("LISTING_MIN_INTERVAL_SECONDS", str(DEFAULT_MIN_INTERVAL))))
    except ValueError:
        return float(DEFAULT_MIN_INTERVAL)


def _load_last() -> float:
    try:
        return float(json.loads(STATE_FILE.read_text()))
    except Exception:
        return 0.0


def seconds_until_slot() -> float:
    """距离可发布还差多少秒;<=0 表示现在可发。"""
    elapsed = time.time() - _load_last()
    return max(0.0, _min_interval() - elapsed)


def wait_for_publish_slot(verbose: bool = True) -> float:
    """阻塞直到满足上架间隔。返回实际等待的秒数。"""
    wait = seconds_until_slot()
    if wait > 0 and verbose:
        print(f"[pacing] 距上次发布不足间隔,等待 {wait:.0f} 秒后再上架(防风控节奏)")
    if wait > 0:
        time.sleep(wait)
    return wait


def mark_published() -> None:
    """发布成功后调用,记录本次时间戳。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(time.time()))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只查看还差多少秒,不等待")
    parser.add_argument("--mark", action="store_true", help="手动记录一次发布时间")
    args = parser.parse_args()
    if args.mark:
        mark_published()
        print("[pacing] 已记录发布时间")
    elif args.check:
        print(f"[pacing] 距可发布还有 {seconds_until_slot():.0f} 秒")
    else:
        waited = wait_for_publish_slot()
        print(f"[pacing] 槽位就绪(等待了 {waited:.0f} 秒)")
