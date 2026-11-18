#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_login.py — 真实扫码登录(浏览器方案): 官方登录页扫码 → 收割 Cookie → 验证。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ddocs.browser_login import browser_login
from ddocs.session import DingSession


def main() -> int:
    s = DingSession(min_interval=0.2)
    s.clear_cookies()  # 从干净状态开始
    try:
        nick = browser_login(s, timeout=300, on_progress=lambda m: print("PROGRESS", m, flush=True))
    except Exception as e:
        print(f"LOGIN_FAILED: {e!r}")
        return 1
    status = s.login_status()
    print(f"LOGIN_OK nick={nick} uid={status.get('uid')} isLogin={status.get('isLogin')}")
    return 0 if status.get("isLogin") else 1


if __name__ == "__main__":
    sys.exit(main())
