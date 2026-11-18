"""会话层 — Cookie/XSRF/a-token 管理 + 响应信封归一 + 温和重试。

认证模型(来自 burp.xml 抓包分析, 见 docs/API_ANALYSIS.md §3):
  1. Cookie 会话: account / dt_s / doc_atoken / portal_corp_id (由扫码登录建立)
  2. CSRF 双提交: Cookie 里的 XSRF-TOKEN → 每个请求头 x-xsrf-token 自动回显
  3. a-token 请求头: POST /portal/api/v1/token/getAccessToken 换取, 401 时自动刷新一次
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from .errors import ApiError, DdocsError, NotLoggedInError

DOCS_ORIGIN = "https://docs.dingtalk.com"
LOGIN_ORIGIN = "https://login.dingtalk.com"
DOCS_CLIENT_ID = "dingoajosaltohiugdhutt"   # docs.dingtalk.com 的 OAuth client_id(抓包常量)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_CRED_FILE = Path.home() / ".ddocs" / "credentials.json"

# 凭证文件里需要保留的 Cookie 键(风控指纹 cookie 如 tfstk/isg/cna 原样携带, 不做解释)
_PERSIST_META = {"saved_at", "user_agent"}


def _now_ms() -> int:
    return int(time.time() * 1000)


class DingSession:
    """持有 httpx 客户端与登录态; 是 SDK 唯一的传输出口。

    Parameters
    ----------
    cred_file: Cookie 持久化路径(默认 ~/.ddocs/credentials.json, 权限 0600)
    min_interval: 相邻 API 调用最小间隔秒数(默认 0.2s, 对无 SLA 的 Web API 保持温柔)
    """

    def __init__(
        self,
        cred_file: Path | str | None = None,
        min_interval: float = 0.2,
        timeout: float = 20.0,
    ):
        self.cred_file = Path(cred_file) if cred_file else DEFAULT_CRED_FILE
        self.min_interval = min_interval
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Referer": f"{DOCS_ORIGIN}/i/desktop"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._access_token: Optional[str] = None
        self._last_call = 0.0
        self.user_agent = USER_AGENT
        self.corp_id: Optional[str] = None   # 组织上下文(浏览器对应 corp-id 头)
        # 自动恢复持久化登录态(若存在)
        self.load_cookies()

    # ---------------------------------------------------------- Cookie 持久化

    def save_cookies(self) -> Path:
        """当前 Cookie Jar → JSON 文件(0600)。包含风控 Cookie, 妥善保管。"""
        cookies = []
        for c in self._http.cookies.jar:
            cookies.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path,
                "expires": c.expires,
            })
        payload = {"saved_at": _now_ms(), "user_agent": self.user_agent, "cookies": cookies}
        self.cred_file.parent.mkdir(parents=True, exist_ok=True)
        self.cred_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.cred_file, 0o600)
        except OSError:  # pragma: no cover - Windows FAT 等场景
            pass
        return self.cred_file

    def load_cookies(self) -> bool:
        """从凭证文件恢复 Cookie; 成功返回 True。"""
        if not self.cred_file.exists():
            return False
        try:
            payload = json.loads(self.cred_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        ua = payload.get("user_agent")
        if ua:
            self.user_agent = ua
            self._http.headers["User-Agent"] = ua
        for c in payload.get("cookies", []):
            self._http.cookies.set(
                c["name"], c["value"], domain=c.get("domain") or "", path=c.get("path") or "/"
            )
        return True

    def clear_cookies(self) -> None:
        self._http.cookies.clear()
        self._access_token = None
        try:
            self.cred_file.unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------------------------------------------------- 底层 HTTP

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _send(self, method: str, url: str, **kw) -> httpx.Response:
        """单次发送, 带温和重试(网络错误/5xx/429 退避重试 3 次)。"""
        last_exc: Exception | None = None
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._http.request(method, url, **kw)
            except httpx.TransportError as e:
                last_exc = e
            else:
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return resp
            time.sleep(0.5 * (attempt + 1))
        raise DdocsError(f"网络请求失败(已重试): {url} ({last_exc})")

    # ---------------------------------------------------------- docs 域 API

    def _xsrf_token(self) -> str:
        """取 docs 域的 XSRF-TOKEN(login 域也有同名 Cookie, 不能直接 get)。"""
        for c in self._http.cookies.jar:
            if c.name == "XSRF-TOKEN" and "docs.dingtalk.com" in (c.domain or ""):
                return c.value or ""
        return ""

    def api(self, method: str, path: str, *, api_host: str = DOCS_ORIGIN, **kw) -> Any:
        """docs 域 JSON API: 自动带 XSRF/corp/a-token 头 → 信封归一 → 失败抛 ApiError。"""
        url = path if path.startswith("http") else f"{api_host}{path}"
        headers = kw.pop("headers", {})
        headers.setdefault("Origin", DOCS_ORIGIN)
        headers.setdefault("Accept", "application/json, text/plain, */*")
        # 网关侧期望的前端默认头(抓包: utm-* 在 128 个请求上出现, 缺失可能 400)
        headers.setdefault("utm-source", "portal")
        headers.setdefault("utm-medium", "portal_i-desktop")
        headers.setdefault("eagleeye-traceid", f"{uuid.uuid4().hex}{int(time.time() * 1000)}")
        if self.corp_id:
            headers.setdefault("corp-id", self.corp_id)
        xsrf = self._xsrf_token()
        if xsrf:
            headers.setdefault("x-xsrf-token", xsrf)
        # a-token 惰性获取(box 等服务校验该头); 获取失败不阻断, 交由 401 重试逻辑兜底
        if self._access_token is None and path != "/portal/api/v1/token/getAccessToken":
            try:
                self.get_access_token()
            except (NotLoggedInError, ApiError, DdocsError):
                pass
        if self._access_token:
            headers.setdefault("a-token", self._access_token)

        resp = self._send(method, url, headers=headers, **kw)

        if resp.status_code == 401 and path != "/portal/api/v1/token/getAccessToken":
            # a-token 过期 → 刷新一次重试
            self._access_token = None
            self.get_access_token(force=True)
            headers = {**headers, "a-token": self._access_token}
            resp = self._send(method, url, headers=headers, **kw)

        return self._unwrap(resp, endpoint=path)

    def _unwrap(self, resp: httpx.Response, *, endpoint: str) -> Any:
        if resp.status_code in (401, 403):
            raise NotLoggedInError(f"登录态失效({resp.status_code}), 请执行 ddocs login")
        if resp.status_code >= 400:
            raise ApiError(f"HTTP {resp.status_code}", status=resp.status_code, endpoint=endpoint)
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            raise ApiError(
                f"非 JSON 响应({ctype[:60]})", status=resp.status_code, endpoint=endpoint
            )
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise ApiError("响应 JSON 解析失败", endpoint=endpoint) from e

        # 信封形态 A: {status, isSuccess, data?}  (docs 主流, 错误时可能无 data)
        if isinstance(body, dict) and "isSuccess" in body:
            if body["isSuccess"] and body.get("status", 200) == 200:
                return body.get("data")
            raise ApiError(
                str(body.get("errorMessage") or body.get("msg") or "业务失败"),
                status=body.get("status", 0), endpoint=endpoint,
            )
        # 信封形态 B: {success, result}  (login 域)
        if isinstance(body, dict) and "success" in body:
            if body["success"]:
                return body.get("result")
            raise ApiError(
                str(body.get("errorMsg") or body.get("errorMessage") or "业务失败"),
                code=str(body.get("errorCode") or body.get("code") or ""),
                endpoint=endpoint,
            )
        # 信封形态 C: 纯错误 {errorCode, errorMessage}
        if isinstance(body, dict) and "errorCode" in body:
            raise ApiError(
                str(body.get("errorMessage") or body.get("errorMsg") or "业务失败"),
                code=str(body["errorCode"]), endpoint=endpoint,
            )
        return body

    # ---------------------------------------------------------- a-token

    def get_access_token(self, force: bool = False) -> str:
        """POST /portal/api/v1/token/getAccessToken → a-token 头的值。"""
        if self._access_token and not force:
            return self._access_token
        resp = self._send("POST", f"{DOCS_ORIGIN}/portal/api/v1/token/getAccessToken",
                          headers={
                              "Origin": DOCS_ORIGIN,
                              "Content-Type": "application/json",
                              "x-xsrf-token": self._xsrf_token(),
                          }, content="{}")
        data = self._unwrap(resp, endpoint="/portal/api/v1/token/getAccessToken")
        token = (data or {}).get("accessToken")
        if not token:
            raise NotLoggedInError("获取 accessToken 失败(Cookie 可能过期), 请 ddocs login")
        self._access_token = token
        return token

    def login_status(self) -> dict:
        """GET /i/api/login/status → {isLogin, uid, ...}"""
        url = f"{DOCS_ORIGIN}/i/api/login/status"
        resp = self._send("GET", url, headers={"Referer": f"{DOCS_ORIGIN}/"})
        return self._unwrap(resp, endpoint="/i/api/login/status") or {}

    def close(self) -> None:
        self._http.close()
