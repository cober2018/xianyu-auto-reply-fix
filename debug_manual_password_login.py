#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import sys
from pathlib import Path


def _read_cookie_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot manual password login debug entry")
    parser.add_argument("--account-id", default="manual_password_debug", help="logical account id for logs and browser profile")
    parser.add_argument("--account", required=True, help="login account")
    parser.add_argument("--password", required=True, help="login password")
    parser.add_argument("--headless", action="store_true", help="run in headless mode")
    parser.add_argument("--show-browser", action="store_true", help="force headed mode")
    parser.add_argument("--force-clean-context", action="store_true", help="use clean browser context instead of persistent profile")
    parser.add_argument("--automation-backend", choices=["auto", "patchright", "playwright"], default="auto")
    parser.add_argument("--stealth-mode", choices=["auto", "off", "lite", "full"], default="auto")
    parser.add_argument("--max-retries", type=int, default=1, help="slider retries for this manual run")
    parser.add_argument("--initial-cookie", help="optional initial cookie string")
    parser.add_argument("--initial-cookie-file", help="optional file containing initial cookie string")
    parser.add_argument("--browser-channel", help="browser channel, such as chrome/msedge")
    parser.add_argument("--browser-path", help="custom browser executable_path")
    parser.add_argument("--proxy-type", default="none", help="none/http/https/socks5")
    parser.add_argument("--proxy-host", default="", help="proxy host")
    parser.add_argument("--proxy-port", type=int, default=0, help="proxy port")
    parser.add_argument("--proxy-user", default="", help="proxy username")
    parser.add_argument("--proxy-pass", default="", help="proxy password")
    parser.add_argument("--verification-wait-timeout", type=int, default=30, help="seconds to wait when QR/face verification is required")
    parser.add_argument("--keep-verification-screenshot", action="store_true", help="do not delete verification screenshot after the run")
    args = parser.parse_args()
    print("密码重登已关闭，请使用扫码登录", file=sys.stderr)
    return 1

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"debug_manual_password_login failed: {exc}", file=sys.stderr)
        raise
