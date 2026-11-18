"""ddocs CLI — 扫码登录 / 列文件 / 收藏 / 下载 / 在线编辑入口。

  ddocs login            扫码登录(终端二维码; --png 另存图片)
  ddocs whoami           当前登录用户
  ddocs ls [UUID]        列目录(默认个人空间根); --cursor 翻页, --json
  ddocs recent           最近访问
  ddocs fav [--top]      收藏列表
  ddocs download UUID    下载文件; -o 目录
  ddocs open UUID        浏览器打开在线编辑/预览
  ddocs logout           清除本地凭证
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table

    _console: Optional["Console"] = Console()
except ImportError:  # rich 为可选依赖
    _console = None

import typer

from .client import DdocsClient
from .errors import DdocsError, NotLoggedInError

app = typer.Typer(add_completion=False, help="钉钉文档网页版命令行(列文件/收藏/下载/扫码登录)")
_typer_app = app


def _client() -> DdocsClient:
    c = DdocsClient()
    if not c.is_logged_in():
        raise NotLoggedInError("未登录, 请先执行: ddocs login")
    return c


def _fmt_time(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _fmt_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}GB"  # pragma: no cover


def _echo(msg: str) -> None:
    if _console:
        _console.print(msg)
    else:
        print(msg)


def _err(msg: str) -> None:
    _echo(f"[red]✘ {msg}[/red]" if _console else f"✘ {msg}")
    raise typer.Exit(1)


_TYPE_ICON = {"folder": "📁", "file": "📄"}


def _print_items(items, json_out: bool) -> None:
    if json_out:
        payload = [i.model_dump() if hasattr(i, "model_dump") else i for i in items]
        # 去掉 extra 原始字典, 避免重复输出
        for p in payload:
            p.pop("raw", None)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if _console:
        table = Table(box=None, header_style="bold")
        for col, justify in (("类型", "center"), ("名称", "left"), ("大小", "right"),
                             ("修改时间", "left"), ("dentryUuid", "left")):
            table.add_column(col, justify=justify, overflow="fold")
        for it in items:
            table.add_row(
                _TYPE_ICON.get(getattr(it, "dentry_type", ""), "📄"),
                getattr(it, "name", ""),
                _fmt_size(getattr(it, "file_size", None)),
                _fmt_time(getattr(it, "updated_time", None)),
                getattr(it, "dentry_uuid", ""),
            )
        _console.print(table)
    else:
        for it in items:
            print(f"{getattr(it, 'dentry_type', 'file'):6} {_fmt_size(getattr(it, 'file_size', None)):>10}"
                  f"  {_fmt_time(getattr(it, 'updated_time', None))}  {getattr(it, 'name', '')}"
                  f"  ({getattr(it, 'dentry_uuid', '')})")


@app.command()
def login(png: Optional[str] = typer.Option(None, "--png", help="仅 --native: 二维码另存 PNG"),
          native: bool = typer.Option(False, "--native", help="纯 HTTP 终端二维码(可能被阿里风控 bx 头拒绝)"),
          wait: float = typer.Option(300.0, help="等待扫码秒数")):
    """扫码登录并持久化 Cookie(~/.ddocs/credentials.json)。默认驱动系统浏览器打开官方登录页。"""
    c = DdocsClient()

    def on_state(msg: str) -> None:
        _echo(f"[dim]{msg}[/dim]" if _console else msg)

    try:
        if native:
            nick = _login_flow(c, wait, png, on_state)
        else:
            from .browser_login import browser_login
            nick = browser_login(c.s, timeout=wait, on_progress=on_state)
        _echo(f"[green]✔ 登录成功: {nick}[/green]" if _console else f"✔ 登录成功: {nick}")
    except DdocsError as e:
        _err(str(e))


def _login_flow(c: DdocsClient, wait: float, png: Optional[str], on_state) -> str:
    from .auth import login as qr_login
    return qr_login(c.s, max_wait=wait, qr_png=png, on_state=on_state)


@app.command()
def logout():
    """清除本地登录凭证。"""
    DdocsClient().s.clear_cookies()
    _echo("已清除本地凭证")


@app.command()
def whoami(json_out: bool = typer.Option(False, "--json")):
    """当前用户与组织。"""
    c = _client()
    me = c.me()
    if json_out:
        print(json.dumps(me.model_dump(exclude={"raw"}), ensure_ascii=False, indent=2, default=str))
        return
    u = me.user
    _echo(f"用户: {u.nick if u else '?'}  uid={u.uid if u else '?'}")
    for o in me.orgs:
        _echo(f"组织: {o.name}  orgId={o.org_id}  corpId={o.corp_id}"
              + ("  [主组织]" if o.is_main_org else ""))


@app.command()
def ls(dentry_uuid: Optional[str] = typer.Argument(None, help="目录 dentryUuid; 缺省=个人空间根目录"),
       cursor: Optional[str] = typer.Option(None, "--cursor", help="翻页游标(上次输出的 loadMoreId)"),
       page_size: int = typer.Option(50, "--page-size"),
       json_out: bool = typer.Option(False, "--json")):
    """列出目录下的文件。"""
    c = _client()
    try:
        page = c.list_folder(dentry_uuid, page_size=page_size, cursor=cursor)
    except (DdocsError, NotLoggedInError) as e:
        _err(str(e))
    items = list(page.children)
    _print_items(items, json_out)
    if page.has_more and page.load_more_id:
        _echo(f"[dim]-- 更多: --cursor {page.load_more_id}[/dim]" if _console
              else f"-- 更多: --cursor {page.load_more_id}")


@app.command()
def recent(page_size: int = typer.Option(20, "--page-size"),
           json_out: bool = typer.Option(False, "--json")):
    """最近访问的文档。"""
    c = _client()
    try:
        items = c.recent(page_size=page_size)
    except DdocsError as e:
        _err(str(e))
    if json_out:
        print(json.dumps([i.model_dump(exclude={"raw"}) for i in items],
                         ensure_ascii=False, indent=2, default=str))
        return
    if _console:
        table = Table(box=None, header_style="bold")
        for col in ("名称", "来源", "访问时间", "dentryUuid"):
            table.add_column(col, overflow="fold")
        for it in items:
            table.add_row(
                it.resource.name if it.resource else "-",
                it.location or "-",
                _fmt_time(it.access_time),
                it.dentry_uuid,
            )
        _console.print(table)
    else:
        for it in items:
            print(f"{it.resource.name if it.resource else '-'}  |  {it.location}  |"
                  f" {it.dentry_uuid}")


@app.command("fav")
def fav(top: bool = typer.Option(False, "--top", help="置顶收藏"),
        json_out: bool = typer.Option(False, "--json")):
    """收藏列表。"""
    c = _client()
    try:
        items = c.favorites(top=top)
    except DdocsError as e:
        _err(str(e))
    _print_items(items, json_out)


@app.command()
def download(dentry_uuid: str = typer.Argument(..., help="文件 dentryUuid"),
             out_dir: Path = typer.Option(Path("."), "-o", "--out-dir"),
             no_guard: bool = typer.Option(False, "--no-guard", help="跳过下载风控检查")):
    """下载文件(流式, 支持 OSS 多分片预签名)。"""
    c = _client()
    try:
        path = c.download(dentry_uuid, dest_dir=out_dir, check_guard=not no_guard)
    except PermissionError as e:
        _err(f"下载被拦截: {e}")
    except DdocsError as e:
        _err(str(e))
    _echo(f"[green]✔ 已下载[/green] {path}" if _console else f"✔ 已下载 {path}")


@app.command()
def sheets(dentry_uuid: str = typer.Argument(..., help="表格文件 dentryUuid")):
    """列出工作表(collab sheetId + 名称)。"""
    c = _client()
    try:
        rows = c.sheets(dentry_uuid)
    except (DdocsError, PermissionError) as e:
        _err(str(e))
    for r in rows:
        _echo(f"{r['id']:>6}  {r['name']}")


@app.command("open")
def open_(dentry_uuid: str = typer.Argument(..., help="文件 dentryUuid"),
          launch_type: str = typer.Option("PREVIEW", "--mode", help="PREVIEW/EDIT")):
    """在浏览器中打开网页版编辑/预览(在线编辑入口)。"""
    c = _client()
    try:
        url = c.open_editor(dentry_uuid, launch_type=launch_type.upper())
    except (DdocsError, RuntimeError) as e:
        _err(str(e))
    _echo(f"✔ 已打开: {url}")


if __name__ == "__main__":
    _typer_app()
