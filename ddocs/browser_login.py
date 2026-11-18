"""浏览器扫码登录 — Playwright 驱动系统 Edge/Chrome 打开官方登录页。

为什么需要浏览器: login.dingtalk.com 的写路径要求阿里 Baxia 风控头
(bx-ua/bx_et/bx-v/bx-umidtoken, 由页面 JS 动态生成设备行为指纹)与
isg/tfstk 风控 Cookie。纯 HTTP 客户端无法自然产生, 也不应伪造。
方案: 二维码仍是官方页面上的官方码, 登录完成后收割 Cookie 交给 DingSession,
后续全部 API 依旧走纯 HTTP。

流程:
  ① 打开 https://docs.dingtalk.com/i/desktop → 未登录自动跳官方登录页(展示二维码)
  ② 用户手机钉钉扫码确认 → 页面回跳 docs.dingtalk.com
  ③ 轮询发现 docs 域会话 Cookie(dt_s + XSRF-TOKEN) → 导入 Jar → login/status 验证
  ④ 持久化 ~/.ddocs/credentials.json
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .errors import DdocsError
from .session import DingSession

log = logging.getLogger("ddocs.browser_login")

LOGIN_ENTRY = "https://docs.dingtalk.com/i/desktop"
_DOCS = "https://docs.dingtalk.com"
_LOGIN = "https://login.dingtalk.com"


def browser_login(session: DingSession, timeout: float = 300.0, *,
                  channel: Optional[str] = None,
                  headful: bool = True,
                  on_progress: Optional[Callable[[str], None]] = None) -> str:
    """打开官方登录页等待扫码, 成功返回用户昵称并持久化 Cookie。

    channel: msedge / chrome / None(依次回退探测)
    """
    say = on_progress or (lambda m: None)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise DdocsError(
            "浏览器登录需要 playwright: pip install playwright"
            "(系统已有 Edge/Chrome 即可, 无需另下浏览器)"
        ) from e

    say("启动浏览器, 打开官方登录页…")
    with sync_playwright() as p:
        browser, last_err = None, None
        for ch in ([channel] if channel else ["msedge", "chrome", None]):
            try:
                browser = p.chromium.launch(channel=ch, headless=not headful)
                break
            except Exception as e:  # 该渠道不可用, 试下一个
                last_err = e
        if browser is None:
            raise DdocsError(f"未找到可用浏览器(需要 Edge 或 Chrome): {last_err}")

        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(LOGIN_ENTRY, wait_until="domcontentloaded", timeout=60_000)
            say(f"登录页已打开({page.url[:80]}…), 请用手机钉钉 [扫一扫] 并在 App 确认")

            deadline = time.monotonic() + timeout
            nick = ""
            probe = 0
            while time.monotonic() < deadline:
                probe += 1
                # 不依赖页面 URL(登录可能开在新标签页): 每轮收割全上下文 Cookie,
                # 用 /i/api/login/status 做权威判定
                all_urls = [p.url for p in ctx.pages] or [LOGIN_ENTRY]
                cookie_urls = [u for u in {_DOCS, _LOGIN}]
                cookies = ctx.cookies(cookie_urls)
                if probe % 5 == 1:  # 诊断(仅键名/URL, 不含值)
                    say(f"等待中… pages={[u[:60] for u in all_urls]} "
                        f"cookies={len(cookies)}")
                if cookies:
                    for c in cookies:
                        session._http.cookies.set(
                            c["name"], c["value"],
                            domain=c.get("domain") or "", path=c.get("path") or "/")
                    try:
                        st = session.login_status()
                    except DdocsError:
                        st = {}
                    if st.get("isLogin"):
                        try:
                            data = session.api("POST", "/api/users/getUserInfo", json={}) or {}
                            nick = ((data.get("user") or {}).get("nick")) or ""
                        except DdocsError:
                            nick = ""
                        session.save_cookies()
                        say(f"登录成功: {nick}, Cookie 已持久化")
                        return nick
                time.sleep(2)
            raise DdocsError("等待扫码超时, 请重试")
        finally:
            browser.close()
