"""扫码登录 — 纯 HTTP 还原 login.dingtalk.com 的二维码登录(无需浏览器)。

流程(来自 burp.xml 抓包, docs/API_ANALYSIS.md §3):
  ① POST /oauth2/generate_qrcode (form)
       → {"success": true, "result": "https://login.dingtalk.com/oauth2/qr_confirm.htm?code=<CODE>"}
     二维码内容即该 URL, 钉钉 App 扫码后在 App 内打开确认页。
  ② 轮询 POST /oauth2/login_with_qr (form: code, stayLogin=true, ...)
       - 未扫码: 继续等待
       - errorCode 11041 → {"errorMsg": "扫描成功"} 已扫码, 待手机端确认
       - errorCode 11021 → 二维码过期, 需重新生成
       - {"success": true, result.pass=true} → 登录成功, Set-Cookie 下发 login 域会话
  ③ GET /oauth2/auth?redirect_uri=https://docs.dingtalk.com/i/&response_type=none
       → 302 链最终落在 docs 域, Set-Cookie 下发 dt_s / XSRF-TOKEN / doc_atoken 等
  ④ GET docs.dingtalk.com/i/api/login/status 验证 isLogin=true
"""

from __future__ import annotations

import time
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

from .errors import DdocsError
from .session import DOCS_CLIENT_ID, DOCS_ORIGIN, LOGIN_ORIGIN, DingSession

REDIRECT_URI = f"{DOCS_ORIGIN}/i/"
POLL_INTERVAL = 1.5
QR_EXPIRED_CODE = "11021"   # 扫码登录失败/过期, 需刷新
QR_SCANNED_CODE = "11041"   # 扫描成功, 等待手机端确认


def _login_form(extra: dict[str, str] | None = None) -> dict[str, str]:
    """login.dingtalk.com 各端点共用的 form 参数(x-www-form-urlencoded)。"""
    form = {
        "redirect_uri": REDIRECT_URI,
        "response_type": "none",
        "client_id": DOCS_CLIENT_ID,
        "scope": "openid",
    }
    if extra:
        form.update(extra)
    return form


class QrLogin:
    """扫码登录状态机。

    Parameters
    ----------
    session: 复用其 httpx 客户端(登录成功的 Cookie 直接落进同一 Jar)
    on_qr:    二维码回调(url, code)。CLI 在这里渲染终端二维码;
              返回 False 可中止登录。
    """

    def __init__(self, session: DingSession):
        self.s = session
        self.code: str = ""
        self.qr_url: str = ""

    # ① 生成二维码
    def generate_qr(self) -> str:
        resp = self.s._send("POST", f"{LOGIN_ORIGIN}/oauth2/generate_qrcode",
                            data=_login_form({
                                "pdmTitle": "Windows Web", "pdmModel": "Windows",
                            }),
                            headers={"Origin": LOGIN_ORIGIN,
                                     "Referer": f"{LOGIN_ORIGIN}/oauth2/challenge.htm"})
        result = self.s._unwrap(resp, endpoint="/oauth2/generate_qrcode")
        if not isinstance(result, str) or "code=" not in result:
            raise DdocsError(f"生成二维码失败: {result!r}")
        self.qr_url = result
        qs = parse_qs(urlsplit(result).query)
        self.code = (qs.get("code") or [""])[0]
        if not self.code:
            raise DdocsError(f"二维码 URL 中没有 code: {result}")
        return self.qr_url

    # ② 轮询扫码结果; 返回 nick(成功) 
    def poll(self, max_wait: float = 180.0,
             on_state: Optional[Callable[[str], None]] = None) -> str:
        deadline = time.monotonic() + max_wait
        scanned_notified = False
        while time.monotonic() < deadline:
            resp = self.s._send("POST", f"{LOGIN_ORIGIN}/oauth2/login_with_qr",
                                data=_login_form({
                                    "code": self.code,
                                    "exclusiveCorpId": "",
                                    "stayLogin": "true",
                                    "pdmTitle": "Windows Web", "pdmModel": "Windows",
                                }),
                                headers={"Origin": LOGIN_ORIGIN,
                                         "Referer": f"{LOGIN_ORIGIN}/oauth2/challenge.htm"})
            body = {}
            try:
                body = resp.json()
            except Exception:
                body = {}
            if body.get("success") is True and (body.get("result") or {}).get("pass"):
                nick = (body.get("result") or {}).get("nick", "")
                return nick
            err = str(body.get("errorCode") or "")
            msg = body.get("errorMsg") or body.get("errorMessage") or ""
            if err == QR_SCANNED_CODE and not scanned_notified:
                scanned_notified = True
                if on_state:
                    on_state(f"已扫码, 请在钉钉 App 上确认({msg or '扫描成功'})…")
            elif err == QR_EXPIRED_CODE:
                raise DdocsError("二维码已过期或确认失败, 请重新执行登录")
            # 其余(含空响应体)视为"等待扫码"
            time.sleep(POLL_INTERVAL)
        raise DdocsError("等待扫码超时, 请重新执行登录")

    # ③ OAuth 回跳 docs 域, 收集会话 Cookie
    def land_docs(self) -> None:
        resp = self.s._http.get(
            f"{LOGIN_ORIGIN}/oauth2/auth",
            params=_login_form(),
            headers={"Origin": LOGIN_ORIGIN, "Referer": f"{DOCS_ORIGIN}/"},
            follow_redirects=True,
        )
        # 手动遍历结束后确认落在 docs 域且 2xx
        if "docs.dingtalk.com" not in str(resp.url):
            raise DdocsError(f"登录回跳未落到 docs 域: {resp.url} (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise DdocsError(f"登录回跳失败: HTTP {resp.status_code}")

    # 完整流程
    def run(self, max_wait: float = 180.0,
            on_qr: Optional[Callable[[str, str], bool]] = None,
            on_state: Optional[Callable[[str], None]] = None) -> str:
        url = self.generate_qr()
        if on_qr and on_qr(url, self.code) is False:
            raise DdocsError("用户取消登录")
        if on_state:
            on_state("等待钉钉 App 扫码…")
        nick = self.poll(max_wait=max_wait, on_state=on_state)
        self.land_docs()
        status = self.s.login_status()
        if not status.get("isLogin"):
            raise DdocsError(f"回跳后 docs 会话未生效: {status}")
        self.s.save_cookies()
        return nick


def render_qr_terminal(url: str) -> None:
    """终端渲染二维码(Unicode 半块字符, 无额外依赖)。"""
    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.print_ascii(invert=True, tty=False)


def render_qr_png(url: str, path: str) -> str:
    """保存二维码 PNG(需要 pillow)。"""
    import qrcode

    try:
        img = qrcode.make(url)
        img.save(path)
    except Exception as e:  # pragma: no cover
        raise DdocsError(
            f"PNG 渲染失败({e}): 请 `pip install pillow` 后重试, 或直接用终端二维码"
        ) from e
    return path


def login(session: DingSession, max_wait: float = 180.0,
          qr_png: Optional[str] = None,
          on_state: Optional[Callable[[str], None]] = None) -> str:
    """顶层扫码登录: 终端渲染二维码 → 轮询 → 回跳 → 校验 → 持久化。返回昵称。"""
    # 预热: 访问登录页收集基础 Cookie(模拟浏览器进入 challenge.htm)
    try:
        session._send("GET", f"{LOGIN_ORIGIN}/oauth2/challenge.htm",
                      params=_login_form(),
                      headers={"Referer": f"{DOCS_ORIGIN}/"})
    except DdocsError:
        pass  # 预热失败不阻断登录

    def _on_qr(url: str, code: str) -> bool:
        if on_state:
            on_state(f"二维码会话 code={code}")
        render_qr_terminal(url)
        if qr_png:
            render_qr_png(url, qr_png)
            if on_state:
                on_state(f"二维码已保存: {qr_png}")
        if on_state:
            on_state("请用手机钉钉 [扫一扫] 扫描上方二维码")
        return True

    flow = QrLogin(session)
    return flow.run(max_wait=max_wait, on_qr=_on_qr, on_state=on_state)
