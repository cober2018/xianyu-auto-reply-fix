#!/usr/bin/env python3
"""xianyu_api.py — 闲鱼自动回复服务(xianyu-auto-reply)的 HTTP 客户端,供 Agent 调用。

用法:
  xianyu_api.py login [--base-url URL] [--username U] [--password P]
  xianyu_api.py get <path>
  xianyu_api.py post <path> [json_body]
  xianyu_api.py put <path> [json_body]
  xianyu_api.py delete <path>
  xianyu_api.py health
  xianyu_api.py accounts                     # GET /cookies/details 全部账号及状态
  xianyu_api.py status <cookie_id>           # GET /cookies/{cid}/runtime-status
  xianyu_api.py qr-generate                  # POST /qr-login/generate
  xianyu_api.py qr-check <session_id>        # GET /qr-login/check/{session_id}
  xianyu_api.py qr-refresh                   # POST /qr-login/refresh-cookies
  xianyu_api.py qr-cooldown <cookie_id>      # GET /qr-login/cooldown-status/{cookie_id}
  xianyu_api.py qr-reset-cooldown <cookie_id>
  xianyu_api.py pwd-login <cookie_id>        # POST /password-login (refresh_mode, 用已存账密)
  xianyu_api.py pwd-check <session_id>       # GET /password-login/check/{session_id}
  xianyu_api.py pwd-cancel <session_id>

凭证保存在 ~/.config/xianyu-skill/credentials.json(权限 0600)。
环境变量 XIANYU_BASE_URL / XIANYU_USERNAME / XIANYU_PASSWORD 优先于已存凭证。
退出码: 0 成功; 1 HTTP 4xx/5xx; 2 连接失败; 3 未登录或凭证缺失。
"""

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = os.environ.get("XIANYU_BASE_URL", "http://127.0.0.1:8090")
CRED_PATH = Path.home() / ".config" / "xianyu-skill" / "credentials.json"
DEFAULT_TIMEOUT = 30


def load_creds():
    if CRED_PATH.exists():
        try:
            return json.loads(CRED_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_creds(creds):
    CRED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CRED_PATH.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CRED_PATH, 0o600)


def resolve_base_url(args, creds):
    return getattr(args, "base_url", None) or os.environ.get("XIANYU_BASE_URL") or creds.get("base_url") or DEFAULT_BASE_URL


def do_login(base_url, username, password, timeout):
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("success") or not data.get("token"):
        raise RuntimeError(data.get("message", "登录失败(未返回 token)"))
    return data


def cmd_login(args):
    creds = load_creds()
    base_url = resolve_base_url(args, creds)
    username = args.username or os.environ.get("XIANYU_USERNAME") or creds.get("username") or input("用户名: ")
    password = args.password or os.environ.get("XIANYU_PASSWORD") or creds.get("password")
    if not password:
        password = getpass.getpass("密码: ")

    try:
        data = do_login(base_url, username, password, args.timeout)
    except (urllib.error.URLError, OSError) as e:
        print(f"连接失败: {e}\n请确认服务已启动且地址正确({base_url})。", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"登录失败: {e}", file=sys.stderr)
        sys.exit(1)

    creds.update(
        base_url=base_url,
        username=username,
        token=data["token"],
        saved_at=data.get("timestamp") or None,
    )
    if args.password or os.environ.get("XIANYU_PASSWORD") or not creds.get("password"):
        # 仅在显式提供或尚未保存过时更新密码(自动重登需要)
        if args.password or os.environ.get("XIANYU_PASSWORD"):
            creds["password"] = password
    save_creds(creds)
    print(json.dumps({"success": True, "message": "登录成功,凭证已保存", "username": username, "base_url": base_url}, ensure_ascii=False))


def request(args, method, path, body=None, _retried=False):
    creds = load_creds()
    base_url = resolve_base_url(args, creds)
    token = creds.get("token")

    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"raw": raw}
        if e.code == 401 and not _retried and creds.get("password"):
            # token 过期 → 自动重登一次并重试
            try:
                login_data = do_login(base_url, creds.get("username", ""), creds["password"], args.timeout)
            except (urllib.error.URLError, OSError, RuntimeError):
                print("HTTP 401,且自动重登失败。请运行: xianyu_api.py login", file=sys.stderr)
                sys.exit(3)
            creds["token"] = login_data["token"]
            save_creds(creds)
            return request(args, method, path, body, _retried=True)
        return e.code, data
    except (urllib.error.URLError, OSError) as e:
        print(f"连接失败: {e}\n请确认服务已启动且地址正确({base_url})。", file=sys.stderr)
        sys.exit(2)


def output(status, data):
    print(json.dumps({"http_status": status, "body": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if 200 <= status < 300 else 1)


def need_auth(args):
    creds = load_creds()
    if not creds.get("token") and not creds.get("password"):
        print("尚未登录。请先运行: xianyu_api.py login", file=sys.stderr)
        sys.exit(3)


def main():
    parser = argparse.ArgumentParser(description="闲鱼自动回复服务 HTTP 客户端")
    parser.add_argument("--base-url", help="服务地址,默认 http://127.0.0.1:8090")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="登录并保存凭证")
    p_login.add_argument("--username")
    p_login.add_argument("--password")
    p_login.set_defaults(func=cmd_login)

    for name, method in [("get", "GET"), ("post", "POST"), ("put", "PUT"), ("delete", "DELETE")]:
        p = sub.add_parser(name, help=f"{method} 请求(通用)")
        p.add_argument("path")
        p.add_argument("body", nargs="?", help="JSON 请求体字符串")
        p.set_defaults(method=method)

    def simple(name, method, path_fn, needs_auth=True):
        p = sub.add_parser(name)
        p.set_defaults(method=method, path_fn=path_fn, simple_body=None, needs_auth=needs_auth)

    simple("health", "GET", lambda a: "/health", needs_auth=False)
    simple("accounts", "GET", lambda a: "/cookies/details")
    simple("status", "GET", lambda a: f"/cookies/{a.arg}/runtime-status")
    simple("qr-generate", "POST", lambda a: "/qr-login/generate")
    simple("qr-check", "GET", lambda a: f"/qr-login/check/{a.arg}")
    simple("qr-refresh", "POST", lambda a: "/qr-login/refresh-cookies")
    simple("qr-cooldown", "GET", lambda a: f"/qr-login/cooldown-status/{a.arg}")
    simple("qr-reset-cooldown", "POST", lambda a: f"/qr-login/reset-cooldown/{a.arg}")
    simple("pwd-login", "POST", lambda a: "/password-login", )
    simple("pwd-check", "GET", lambda a: f"/password-login/check/{a.arg}")
    simple("pwd-cancel", "POST", lambda a: f"/password-login/cancel/{a.arg}")

    for name in ["status", "qr-check", "qr-cooldown", "qr-reset-cooldown", "pwd-check", "pwd-cancel"]:
        sub.choices[name].add_argument("arg", help="cookie_id 或 session_id")
    sub.choices["pwd-login"].add_argument("arg", help="account_id (cookie_id)")
    sub.choices["pwd-login"].set_defaults(
        simple_body={"account_id": "@arg", "refresh_mode": True})

    args = parser.parse_args()

    if hasattr(args, "func"):  # login
        args.func(args)
        return

    if getattr(args, "needs_auth", True):
        need_auth(args)

    body = getattr(args, "body", None)
    if body is None and getattr(args, "simple_body", None):
        body = json.loads(json.dumps(args.simple_body).replace('"@arg"', json.dumps(args.arg)))
    elif body is not None:
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            print("请求体不是合法 JSON。", file=sys.stderr)
            sys.exit(1)

    path = args.path_fn(args) if hasattr(args, "path_fn") else args.path
    status, data = request(args, args.method, path, body)
    output(status, data)


if __name__ == "__main__":
    main()
