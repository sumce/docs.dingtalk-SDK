"""离线回放测试 — respx 按 burp.xml 抓包的真实响应形态 mock, 凭证均为假值。"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from ddocs.auth import QrLogin
from ddocs.client import DdocsClient, parse_presigned_filename
from ddocs.errors import ApiError, NotLoggedInError
from ddocs.session import DingSession

# ------------------------------------------------------------ 抓包形态样本

DOC_ENVELOPE_OK = {"status": 200, "isSuccess": True, "data": {"k": "v"}}
DOC_ENVELOPE_ERR = {"status": 500, "isSuccess": False, "errorMessage": "业务失败"}
DOC_ENVELOPE_401 = {"status": 401, "code": "52600001", "timestamp": 1794914532483}

DENTRY_SAMPLE = {
    "dentryUuid": "uuidA1b2C3d4", "dentryId": "230870005112",
    "dentryKey": "23GmzGPVtPEXAOEY", "docKey": "4j6OJ5jwAo0zgq3p",
    "name": "示例表格.xlsx", "dentryType": "file", "contentType": "document",
    "extension": "xlsx", "fileSize": 6286694, "hasChildren": False,
    "createdTime": 1785578757000, "updatedTime": 1790209945000,
    "creator": {"name": "张三", "uid": "1251533910"},
    "driveSpaceId": "28638605156", "corpId": "dingfake", "orgId": "885844352",
    "dentryStatistic": {"childrenCount": 0, "downloadCount": 376, "previewCount": 621},
}

PRESIGNED = (
    "https://zjk-dualstack.trans.dingtalk.com/yundisk0/xx.file"
    "?Expires=1794915496&OSSAccessKeyId=LTAIfake&Signature=fake%3D"
    "&response-content-disposition=attachment%3B%20filename%3D%22%25E5%258C%2597%25E4%25BA%25AC.xlsx%22"
    "%3B%20filename%2A%3DUTF-8%27%27%25E5%258C%2597%25E4%25BA%25AC%2520%25E6%25B5%258B%25E8%25AF%2595.xlsx"
)


def make_client(tmp_path) -> DdocsClient:
    s = DingSession(cred_file=tmp_path / "creds.json", min_interval=0.0)
    # 预置上下文, 短路 _ensure_org / 惰性 a-token(那些端点未被 mock)
    s.corp_id = "dingfake"
    s._access_token = "fake-token"
    return DdocsClient(session=s)


# ------------------------------------------------------------ 信封归一 / XSRF

@respx.mock(assert_all_called=False)
def test_envelope_ok_and_xsrf_echo(respx_mock):
    route = respx_mock.post("https://docs.dingtalk.com/api/users/getUserInfo").respond(
        json=DOC_ENVELOPE_OK
    )
    s = DingSession(cred_file="unused.json", min_interval=0.0)
    s._access_token = "fake-token"   # 短路惰性获取
    s._http.cookies.set("XSRF-TOKEN", "xsrf-123", domain="docs.dingtalk.com")
    s.api("POST", "/api/users/getUserInfo", json={})
    sent = route.calls.last.request
    assert sent.headers["x-xsrf-token"] == "xsrf-123"
    assert sent.headers["origin"] == "https://docs.dingtalk.com"


@respx.mock(assert_all_called=False)
def test_envelope_error(respx_mock):
    respx_mock.post("https://docs.dingtalk.com/api/users/getUserInfo").respond(json=DOC_ENVELOPE_ERR)
    s = DingSession(cred_file="unused.json", min_interval=0.0)
    s._access_token = "fake-token"   # 短路惰性获取
    with pytest.raises(ApiError):
        s.api("POST", "/api/users/getUserInfo", json={})


@respx.mock(assert_all_called=False)
def test_401_raises_not_logged_in(respx_mock):
    respx_mock.post("https://docs.dingtalk.com/api/users/getUserInfo").respond(
        json=DOC_ENVELOPE_401, status_code=401
    )
    # api() 在 401 时会先尝试刷新 a-token; Cookie 已死 → 刷新同样 401
    respx_mock.post("https://docs.dingtalk.com/portal/api/v1/token/getAccessToken").respond(
        json=DOC_ENVELOPE_401, status_code=401
    )
    s = DingSession(cred_file="unused.json", min_interval=0.0)
    with pytest.raises(NotLoggedInError):
        s.api("POST", "/api/users/getUserInfo", json={})


# ------------------------------------------------------------ 扫码登录状态机

@respx.mock(assert_all_called=False)
def test_qr_login_flow(tmp_path, respx_mock):
    # ① 生成二维码
    respx_mock.post("https://login.dingtalk.com/oauth2/generate_qrcode").respond(
        json={"success": True, "result": "https://login.dingtalk.com/oauth2/qr_confirm.htm?code=CODE-1"}
    )
    # 登录页预热
    respx_mock.get("https://login.dingtalk.com/oauth2/challenge.htm").respond(html="<html></html>")
    # ② 轮询: 第一次 11041(已扫码), 第二次成功(带 Set-Cookie)
    qr_route = respx_mock.post("https://login.dingtalk.com/oauth2/login_with_qr")
    qr_route.side_effect = [
        httpx.Response(200, json={"success": False, "errorCode": "11041", "errorMsg": "扫描成功"}),
        httpx.Response(200, json={"success": True, "result": {"pass": True, "nick": "测试用户"}},
                       headers={"Set-Cookie": "account=fake-oauth-account; Domain=.dingtalk.com; Path=/"}),
    ]
    # ③ 回跳链: 302 → 302 → 200(docs 域)
    respx_mock.get("https://login.dingtalk.com/oauth2/auth").respond(
        status_code=302, headers={"Location": "https://login.dingtalk.com/oauth2/pass.htm"}
    )
    respx_mock.get("https://login.dingtalk.com/oauth2/pass.htm").respond(
        status_code=302, headers={"Location": "https://docs.dingtalk.com/i/"}
    )
    respx_mock.get("https://docs.dingtalk.com/i/").respond(
        html="<html>ok</html>",
        headers={"Set-Cookie": "dt_s=fake-session; Domain=docs.dingtalk.com; Path=/"},
    )
    # ④ 会话校验
    respx_mock.get("https://docs.dingtalk.com/i/api/login/status").respond(
        json={"status": 200, "isSuccess": True, "data": {"isLogin": True, "uid": 1}}
    )

    s = DingSession(cred_file=tmp_path / "creds.json", min_interval=0.0)
    flow = QrLogin(s)
    nick = flow.run(max_wait=3.0)
    assert nick == "测试用户"
    # Domain=docs.dingtalk.com 经 RFC 归一后带前导点
    assert s._http.cookies.get("dt_s", domain=".docs.dingtalk.com") == "fake-session"
    # 凭证已持久化
    payload = json.loads((tmp_path / "creds.json").read_text(encoding="utf-8"))
    assert any(c["name"] == "dt_s" for c in payload["cookies"])
    # 轮询请求带上了 code/stayLogin
    form = dict(parse_qsl(qr_route.calls[1].request.content.decode()))
    assert form["code"] == "CODE-1" and form["stayLogin"] == "true"

@respx.mock(assert_all_called=False)
def test_qr_expired_raises(respx_mock):
    respx_mock.post("https://login.dingtalk.com/oauth2/generate_qrcode").respond(
        json={"success": True, "result": "https://login.dingtalk.com/oauth2/qr_confirm.htm?code=CODE-X"}
    )
    respx_mock.post("https://login.dingtalk.com/oauth2/login_with_qr").respond(
        json={"success": False, "errorCode": "11021", "errorMsg": "扫码登录失败"}
    )
    s = DingSession(cred_file="unused.json", min_interval=0.0)
    with pytest.raises(Exception, match="过期"):
        QrLogin(s).run(max_wait=1.0)


# ------------------------------------------------------------ 列文件 / 收藏

@respx.mock(assert_all_called=False)
def test_ls_and_cursor(respx_mock, tmp_path):
    c = make_client(tmp_path)
    respx_mock.get("https://docs.dingtalk.com/box/api/v1/mine/space/info").respond(
        json={"status": 200, "isSuccess": True, "data": {"spaceUuid": "sp1",
               "rootDentryUuid": "ROOT-UUID", "name": "我的文档"}}
    )
    route = respx_mock.get("https://docs.dingtalk.com/box/api/v2/dentry/list").respond(
        json={"status": 200, "isSuccess": True, "data": {
            "children": [DENTRY_SAMPLE], "hasMore": True, "loadMoreId": "CUR-2",
            "root": {"dentryUuid": "ROOT-UUID", "name": "我的文档", "dentryType": "folder"},
        }}
    )
    page = c.list_folder()  # 不传 uuid → 个人空间根
    req_url = str(route.calls.last.request.url)
    assert "dentryUuid=ROOT-UUID" in req_url and "pageSize=50" in req_url
    assert page.children[0].dentry_uuid == "uuidA1b2C3d4"
    assert page.children[0].statistic.download_count == 376
    assert page.children[0].name == "示例表格.xlsx"
    assert page.has_more and page.load_more_id == "CUR-2"
    # 翻页
    c.list_folder(cursor=page.load_more_id)
    assert "loadMoreId=CUR-2" in str(route.calls[-1].request.url)


@respx.mock(assert_all_called=False)
def test_favorites(respx_mock, tmp_path):
    c = make_client(tmp_path)
    route = respx_mock.get("https://docs.dingtalk.com/box/api/v2/star/list").respond(
        json={"status": 200, "isSuccess": True, "data": {
            "hasMore": False,
            "list": [{**DENTRY_SAMPLE, "starredTime": 1794914000000, "resourceType": "DENTRY"}],
        }}
    )
    items = c.favorites()
    assert items[0].dentry_uuid == "uuidA1b2C3d4"
    q = str(route.calls.last.request.url)
    assert "contentTypes=doc%2Cfolder" in q and "supportResourceTypes=DENTRY%2CWORKSPACE%2CTEAM" in q


# ------------------------------------------------------------ 下载

@respx.mock(assert_all_called=False)
def test_download(tmp_path, respx_mock):
    c = make_client(tmp_path)
    guard_route = respx_mock.post(
        "https://docs.dingtalk.com/box/api/v1/dentry/operationGuard"
    ).respond(json={"status": 200, "isSuccess": True, "data": {"isBlocked": False}})
    respx_mock.get("https://docs.dingtalk.com/box/api/v2/file/download").respond(
        json={"status": 200, "isSuccess": True, "data": {
            "downloadType": "URL_PRE_SIGNATURE",
            "ossUrlPreSignatureInfo": {"partSize": 1, "preSignUrls": [PRESIGNED]},
        }}
    )
    respx_mock.get("https://zjk-dualstack.trans.dingtalk.com/yundisk0/xx.file").respond(
        content=b"excel-bytes-here"
    )

    out = c.download("uuidA1b2C3d4", dest_dir=tmp_path)
    assert out.name == "北京 测试.xlsx"           # filename* 解析
    assert out.read_bytes() == b"excel-bytes-here"
    assert not out.with_suffix(".xlsx.part").exists()

    info = c.download_info("uuidA1b2C3d4")
    assert len(info.pre_sign_urls) == 1
    assert guard_route.calls and b"uuidA1b2C3d4" in guard_route.calls.last.request.content


def test_parse_presigned_filename():
    assert parse_presigned_filename(PRESIGNED) == "北京 测试.xlsx"
    simple = ("https://x.com/f?Signature=a"
              "&response-content-disposition=attachment%3B%20filename%3D%22plain.bin%22")
    assert parse_presigned_filename(simple) == "plain.bin"
    assert parse_presigned_filename("https://x.com/f?Signature=a") == ""


# ------------------------------------------------------------ 列出工作表

def test_sheets_via_xlsx(tmp_path, respx_mock):
    """sheets(): 下载 xlsx → openpyxl 读 sheet 名 → 映射 sN。"""
    import openpyxl

    xlsx = tmp_path / "wb.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "汇总"
    wb.create_sheet("明细")
    wb.save(xlsx)
    xbytes = xlsx.read_bytes()

    c = make_client(tmp_path)
    respx_mock.get("https://docs.dingtalk.com/box/api/v2/dentry/info").respond(
        json={"status": 200, "isSuccess": True,
              "data": {"dentryUuid": "uuidA1b2C3d4", "name": "表.xlsx",
                       "dentryType": "file", "extension": "xlsx"}}
    )
    respx_mock.post("https://docs.dingtalk.com/box/api/v1/dentry/operationGuard").respond(
        json={"status": 200, "isSuccess": True, "data": {"isBlocked": False}}
    )
    respx_mock.get("https://docs.dingtalk.com/box/api/v2/file/download").respond(
        json={"status": 200, "isSuccess": True, "data": {
            "downloadType": "URL_PRE_SIGNATURE",
            "ossUrlPreSignatureInfo": {"partSize": 1, "preSignUrls": [PRESIGNED]}}}
    )
    # 用真实 xlsx 字节替换预签名 URL 的响应
    respx_mock.get("https://zjk-dualstack.trans.dingtalk.com/yundisk0/xx.file").respond(content=xbytes)

    rows = c.sheets("uuidA1b2C3d4")
    assert rows == [{"id": "s1", "name": "汇总", "index": 0},
                    {"id": "s2", "name": "明细", "index": 1}]


# ------------------------------------------------------------ 编辑入口

@respx.mock(assert_all_called=False)
def test_editor_url(respx_mock, tmp_path):
    c = make_client(tmp_path)
    respx_mock.post("https://docs.dingtalk.com/box/api/v2/dentry/launch").respond(
        json={"status": 200, "isSuccess": True, "data": {
            "contentMode": "alidoc",
            "launchInfo": {"pcUrl": "https://alidocs.dingtalk.com/i/nodes/xxx?editable=true"},
            "dentry": DENTRY_SAMPLE,
        }}
    )
    url = c.editor_url("uuidA1b2C3d4")
    assert url.startswith("https://alidocs.dingtalk.com")
    body = respx_mock.calls.last.request.content.decode()
    assert json.loads(body)["launchType"] == "PREVIEW"
