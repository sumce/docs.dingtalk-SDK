"""业务客户端 — 列文件 / 收藏 / 下载 / 编辑入口(范围见抓包分析 docs/API_ANALYSIS.md)。

覆盖端点(全部来自真实抓包):
  POST /api/users/getUserInfo                    → me()
  GET  /box/api/v1/mine/space/info               → space_info()
  GET  /box/api/v2/dentry/list                   → list_folder()
  GET  /box/api/v2/dentry/info                   → get_dentry()
  GET  /box/api/v2/star/list | star/top/list     → favorites()
  GET  /portal/api/v1/recent/list                → recent()
  POST /box/api/v1/dentry/operationGuard         → guard_download()
  GET  /box/api/v2/file/download                 → download_info() / download()
  POST /box/api/v2/dentry/launch                 → open_editor()
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from .errors import ApiError, DdocsError
from .models import Dentry, DentryPage, DownloadInfo, RecentItem, SpaceInfo, StarItem, UserInfo
from .session import DOCS_ORIGIN, DingSession

STAR_CONTENT_TYPES = "doc,folder,image,mind,notable,other,pdf,ppt,sheet,video,whiteboard,workspace"


def parse_presigned_filename(url: str) -> str:
    """解析 OSS 预签名 URL 中 response-content-disposition 里的文件名。

    真实样例(抓包): ...&response-content-disposition=attachment%3B%20filename%3D%22..%22
                    %3B%20filename%2A%3DUTF-8%27%27%25E5%258C%2597...xlsx
    filename*(RFC5987) 优先于 filename; parse_qsl 已完成一层 URL 解码。
    """
    disp = ""
    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if k == "response-content-disposition":
            disp = v
            break
    if not disp:
        return ""
    # disposition 值在 URL query 里是双重编码, parse_qsl 解一层, 捕获后再解一层
    m = re.search(r"filename\*\s*=(?:[^']*'')?([^;]+)", disp, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*="?([^";]+)"?', disp, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip())
    return ""


class DdocsClient:
    """钉钉文档网页版客户端(同步)。

    Example
    -------
    >>> from ddocs import DdocsClient
    >>> c = DdocsClient()          # 自动加载 ~/.ddocs/credentials.json
    >>> c.me().user.nick
    """

    def __init__(self, session: Optional[DingSession] = None,
                 cred_file: str | Path | None = None):
        self.s = session or DingSession(cred_file=cred_file)
        self._space_cache: Optional[SpaceInfo] = None
        self._me: Optional[UserInfo] = None

    def _ensure_org(self) -> None:
        """box 服务要求 corp-id 头(实测缺失 400): 惰性取一次用户主组织。"""
        if self.s.corp_id:
            return
        try:
            self.me()
        except Exception:
            pass

    # ------------------------------------------------------------ 登录态

    @staticmethod
    def login(cred_file: str | Path | None = None, max_wait: float = 180.0,
              qr_png: Optional[str] = None, on_state=None) -> "DdocsClient":
        """扫码登录并返回已登录客户端。"""
        from .auth import login as qr_login

        c = DdocsClient(cred_file=cred_file)
        qr_login(c.s, max_wait=max_wait, qr_png=qr_png, on_state=on_state)
        return c

    def is_logged_in(self) -> bool:
        try:
            return bool(self.s.login_status().get("isLogin"))
        except Exception:
            return False

    def me(self) -> UserInfo:
        data = self.s.api("POST", "/api/users/getUserInfo", json={}) or {}
        info = UserInfo.model_validate(data)
        self._me = info
        # 主组织作为后续请求的 corp-id 上下文(抓包: box 服务请求带 corp-id 头)
        if not self.s.corp_id:
            for o in info.orgs:
                if o.is_main_org and o.corp_id:
                    self.s.corp_id = o.corp_id
                    break
        return info

    # ------------------------------------------------------------ 空间/文件

    def space_info(self, refresh: bool = False) -> SpaceInfo:
        """个人空间信息(含 rootDentryUuid — `ls` 的默认根)。"""
        if self._space_cache is not None and not refresh:
            return self._space_cache
        self._ensure_org()
        data = self.s.api("GET", "/box/api/v1/mine/space/info") or {}
        self._space_cache = SpaceInfo.model_validate(data)
        return self._space_cache

    def get_dentry(self, dentry_uuid: str) -> Dentry:
        self._ensure_org()
        data = self.s.api("GET", "/box/api/v2/dentry/info",
                          params={"dentryUuid": dentry_uuid}) or {}
        return Dentry.model_validate(data)

    def list_folder(self, dentry_uuid: Optional[str] = None, *,
                    page_size: int = 50, cursor: Optional[str] = None,
                    order: str = "SORT_KEY", desc: bool = True) -> DentryPage:
        """列目录。不传 dentryUuid 时列个人空间根目录。

        分页: hasMore=True 时用返回的 load_more_id 作为下一次的 cursor。
        """
        if dentry_uuid is None:
            dentry_uuid = self.space_info().root_dentry_uuid
        self._ensure_org()
        params: dict = {
            "dentryUuid": dentry_uuid,
            "withParentAncestors": "true",
            "orderType": order,
            "sortType": "desc" if desc else "asc",
            "listDentrySource": "2",
            "pageSize": str(page_size),
        }
        if cursor:
            params["loadMoreId"] = cursor
        data = self.s.api("GET", "/box/api/v2/dentry/list", params=params) or {}
        return DentryPage.model_validate(data)

    def iter_folder(self, dentry_uuid: Optional[str] = None, *,
                    page_size: int = 50) -> Iterator[Dentry]:
        """目录全量遍历生成器(自动翻页, 每页之间保持限速)。"""
        cursor: Optional[str] = None
        while True:
            page = self.list_folder(dentry_uuid, page_size=page_size, cursor=cursor)
            yield from page.children
            if not page.has_more or not page.load_more_id:
                break
            cursor = page.load_more_id

    def recent(self, page_size: int = 20) -> list[RecentItem]:
        data = self.s.api("GET", "/portal/api/v1/recent/list", params={
            "pageSize": str(page_size),
            "resourceCorpId": "all",
            "operateTypes": "ACCESS",
            "creatorType": "0",
            "excludeTypesInOther": "14",
        }) or {}
        return [RecentItem.model_validate(x) for x in (data.get("list") or [])]

    # ------------------------------------------------------------ 收藏

    def favorites(self, top: bool = False,
                  resource_types: str = "DENTRY,WORKSPACE,TEAM") -> list[StarItem]:
        """收藏列表。top=True 取置顶(star/top/list)。"""
        self._ensure_org()
        path = "/box/api/v2/star/top/list" if top else "/box/api/v2/star/list"
        data = self.s.api("GET", path, params={
            "contentTypes": STAR_CONTENT_TYPES,
            "excludeTypesInOther": "pdf",
            "supportResourceTypes": resource_types,
        }) or {}
        return [StarItem.model_validate(x) for x in (data.get("list") or [])]

    def is_starred(self, dentry_uuid: str) -> bool:
        data = self.s.api("GET", "/portal/api/v1/star/get",
                          params={"dentryUuid": dentry_uuid}) or {}
        return bool(data.get("starred"))

    # ------------------------------------------------------------ 下载

    def guard_download(self, dentry_uuid: str, corp_id: str = "") -> bool:
        """下载前置风控检查 → True 表示被组织策略拦截。"""
        self._ensure_org()
        headers = {"corp-id": corp_id} if corp_id else {}
        data = self.s.api("POST", "/box/api/v1/dentry/operationGuard",
                          json={
                              "operationType": "DOWNLOAD",
                              "resourceType": 0,
                              "resourceIdList": [dentry_uuid],
                              "includeChildren": True,
                          }, headers=headers) or {}
        return bool(data.get("isBlocked"))

    def download_info(self, dentry_uuid: str) -> DownloadInfo:
        """获取 OSS 预签名下载信息(多分片时 pre_sign_urls 有多个)。"""
        self._ensure_org()
        data = self.s.api("GET", "/box/api/v2/file/download", params={
            "dentryUuid": dentry_uuid,
            "supportDownloadTypes": "URL_PRE_SIGNATURE,HTTP_TO_CENTER",
            "downloadType": "URL_PRE_SIGNATURE",
        }) or {}
        info = (data.get("ossUrlPreSignatureInfo") or {}) if isinstance(data, dict) else {}
        urls = info.get("preSignUrls") or []
        if not urls and isinstance(data, dict):
            urls = data.get("preSignUrls") or []
        name = parse_presigned_filename(urls[0]) if urls else ""
        return DownloadInfo(
            download_type=data.get("downloadType", "URL_PRE_SIGNATURE"),
            part_size=int(info.get("partSize") or 1),
            pre_sign_urls=[u for u in urls if u],
            file_name=name,
        )

    def download(self, dentry_uuid: str, dest_dir: str | Path = ".",
                 dest_name: Optional[str] = None, *, check_guard: bool = True,
                 chunk_size: int = 1 << 20, on_progress=None) -> Path:
        """下载文件到 dest_dir。

        - 默认先过 operationGuard(组织禁止下载时抛 DdocsError)
        - 文件名优先级: dest_name > 预签名 URL content-disposition > dentry.name
        - 预签名 URL 有效期有限(约 15 分钟), 立即下载
        """
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        if check_guard and self.guard_download(dentry_uuid):
            raise PermissionError(f"组织安全策略禁止下载: {dentry_uuid}")

        try:
            info = self.download_info(dentry_uuid)
        except ApiError as e:
            if e.status == 403:
                raise PermissionError(
                    "该文档不允许预签名下载(在线文档 alidoc 类需走导出, 或组织策略禁止)"
                ) from e
            raise
        if not info.pre_sign_urls:
            raise RuntimeError(f"未获取到下载地址: {dentry_uuid}")

        if dest_name is None:
            dest_name = info.file_name
        if not dest_name:
            try:
                dest_name = self.get_dentry(dentry_uuid).name or f"{dentry_uuid}.bin"
            except Exception:
                dest_name = f"{dentry_uuid}.bin"
        out = dest / dest_name

        # 单分片: 直接流式下载。多分片(大文件): 预签名 URL 顺序拼接(各分片按序追加)
        tmp = out.with_suffix(out.suffix + ".part")
        total = 0
        with tmp.open("wb") as fh:
            for i, url in enumerate(info.pre_sign_urls if len(info.pre_sign_urls) > 1
                                    else info.pre_sign_urls[:1]):
                with self.s._http.stream("GET", url) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes(chunk_size):
                        fh.write(chunk)
                        total += len(chunk)
                        if on_progress:
                            on_progress(i, len(info.pre_sign_urls), total)
                if len(info.pre_sign_urls) > 1:
                    time.sleep(0.2)  # 分片之间稍作停顿
        tmp.replace(out)
        return out

    def sheets(self, dentry_uuid: str, *, keep: bool = False) -> list[dict]:
        """列出工作表(xlsx/xls)。

        实现说明: sheet **名字**存在于端到端加密的文档 skeleton 中
        (AES-GCM/KC 密钥交换, 见 docs/API_ANALYSIS.md §5), 协议级解密需要
        前端 JS 的密钥推导算法, 不在抓包能力范围内。因此采用等价方案:
        下载原始 xlsx → openpyxl 读 sheet 名(顺序) → 按 extraPartsOssKey
        观测到的键序映射 collab 通道的 sheetId(s1=第1个, s2=第2个...)。

        返回: [{"id": "s1", "name": "汇总", "index": 0}, ...]
        keep=True 时保留下载的临时文件(默认删除)。
        """
        import tempfile

        try:
            import openpyxl  # noqa: F401
        except ImportError as e:
            raise DdocsError(
                "列出工作表需要 openpyxl: pip install openpyxl (或 ddocs[sheets])"
            ) from e

        d = self.get_dentry(dentry_uuid)
        if (d.extension or "").lower() not in ("xlsx", "xlsm", "xls"):
            raise DdocsError(
                f"仅支持表格类文件(当前 extension={d.extension or '?'}); "
                "在线文档的 sheet 清单在加密 skeleton 中"
            )

        with tempfile.TemporaryDirectory(prefix="ddocs-sheets-") as td:
            path = self.download(dentry_uuid, dest_dir=td)
            if keep:
                import shutil
                dest = Path(td).parent / path.name
                shutil.copy(path, dest)
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True)
            names = wb.sheetnames
            wb.close()
        return [{"id": f"s{i + 1}", "name": n, "index": i} for i, n in enumerate(names)]

    # ------------------------------------------------------------ 读取(协议级)

    def get_deltas(self, doc_key: str, version: int = 0, *, max_pages: int = 20) -> list[dict]:
        """拉取 doc_key 自 version 起的明文 OT 操作流(自动翻页)。

        GET collab.dingtalk.com/api/collab/v2/getDeltasWithVersion
        (请求头 a-doc-key 携带文档上下文; 操作未加密, 抓包验证)
        """
        out: list[dict] = []
        cur = version
        for _ in range(max_pages):
            data = self.s.api(
                "GET", "https://collab.dingtalk.com/api/collab/v2/getDeltasWithVersion",
                params={"version": str(cur), "limit": "500"},
                headers={"a-doc-key": doc_key,
                         "Referer": f"{DOCS_ORIGIN}/"},
            ) or {}
            batch = data.get("list") or []
            out.extend(batch)
            if len(batch) < 500 or not batch:
                break
            cur = max((e.get("version") or cur) for e in batch)
        return out

    def read_cells(self, dentry_uuid: str, sheet_id: Optional[str] = None,
                   *, since_version: int = 0) -> dict[str, dict]:
        """协议级读取单元格内容(全量重放 delta, 不经加密 skeleton)。

        返回 {sheetId: {"r行c列": {"value":..., "formula":...}}}
        注意: 只能重建 delta 流覆盖的状态 —— 从 version 0 全量拉取时即完整
        文档内容; 文档很老且已 checkpoint 归档的部分可能不在流里(届时以
        since_version 传入 checkpoint 版本, 或改走 xlsx 下载路线)。
        """
        from .replay import replay_deltas

        self._ensure_org()
        d = self.get_dentry(dentry_uuid)
        doc_key = d.doc_key or d.dentry_key
        if not doc_key:
            raise DdocsError(f"dentry 缺少 docKey: {dentry_uuid}")
        deltas = self.get_deltas(doc_key, since_version)
        state = replay_deltas(deltas, doc_key=doc_key)
        return state.read_cells(sheet_id)

    def sheets_protocol(self, dentry_uuid: str) -> list[dict]:
        """协议级列出工作表(从 delta 流重建): [{id, title, cells}]。

        title 只覆盖 delta 流中出现过 addSheet/updateSheet 的工作表;
        导入 xlsx 的原始 sheet 名在加密 skeleton 中, 用 sheets() 获取。
        """
        from .replay import replay_deltas

        self._ensure_org()
        d = self.get_dentry(dentry_uuid)
        doc_key = d.doc_key or d.dentry_key
        deltas = self.get_deltas(doc_key, 0)
        state = replay_deltas(deltas, doc_key=doc_key)
        return state.sheet_titles()

    # ------------------------------------------------------------ 编辑入口

    def editor_url(self, dentry_uuid: str, launch_type: str = "PREVIEW") -> str:
        """launch 打开文档 → 从 launchInfo 中提取网页版编辑/预览 URL。

        launch_type: PREVIEW 预览 | EDIT 编辑(以服务端返回的实际 URL 为准)
        """
        data = self.s.api("POST", "/box/api/v2/dentry/launch", json={
            "dentryUuid": dentry_uuid,
            "launchType": launch_type,
            "extraParam": {"scene": "universalSpace", "mainsiteOrigin": "mainsite"},
        }) or {}
        url = _find_editor_url(data)
        if not url:
            raise RuntimeError(f"launch 响应中未找到编辑器 URL: {dentry_uuid}")
        return url

    def open_editor(self, dentry_uuid: str, launch_type: str = "PREVIEW") -> str:
        """获取编辑 URL 并调用系统浏览器打开(在线编辑的落地方案)。返回 URL。"""
        import webbrowser

        url = self.editor_url(dentry_uuid, launch_type)
        webbrowser.open(url)
        return url

    def close(self) -> None:
        self.s.close()


def _find_editor_url(node, _depth: int = 0) -> str:
    """在 launch 响应里递归找第一个文档 URL。"""
    if _depth > 6:
        return ""
    if isinstance(node, str):
        if node.startswith("http") and ("dingtalk.com" in node or "alidocs" in node):
            return node
        return ""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and ("url" in k.lower() or "link" in k.lower()):
                found = _find_editor_url(v, _depth + 1)
                if found:
                    return found
        for v in node.values():
            found = _find_editor_url(v, _depth + 1)
            if found:
                return found
    if isinstance(node, list):
        for v in node:
            found = _find_editor_url(v, _depth + 1)
            if found:
                return found
    return ""
