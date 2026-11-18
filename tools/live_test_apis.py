#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_test_apis.py — 真实环境全链路测试(只读): whoami/ls/fav/recent/download。"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ddocs import DdocsClient

OUT = Path(__file__).resolve().parents[1] / "downloads"


def fmt_size(n):
    if n is None:
        return "-"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f}{u}" if u != "B" else f"{n}B"
        n /= 1024
    return "-"


def main() -> int:
    c = DdocsClient()
    ok = []

    # 1. 会话
    st = c.s.login_status()
    print(f"[1] login_status: isLogin={st.get('isLogin')} uid={st.get('uid')}")
    ok.append(bool(st.get("isLogin")))

    # 2. whoami
    me = c.me()
    print(f"[2] whoami: {me.user.nick if me.user else '?'}  orgs={[o.name for o in me.orgs]}")
    ok.append(bool(me.user))

    # 3. 个人空间
    sp = c.space_info()
    print(f"[3] space: {sp.name} root={sp.root_dentry_uuid} quota={sp.used_quota}/{sp.max_quota}")
    ok.append(bool(sp.root_dentry_uuid))

    # 4. ls 根目录
    page = c.list_folder()
    print(f"[4] ls root: {len(page.children)} 项 (hasMore={page.has_more})")
    for f in page.children[:10]:
        print(f"     {f.dentry_type:6} {fmt_size(f.file_size):>9}  {f.name[:40]:42} {f.dentry_uuid[:16]}")
    ok.append(True)

    # 5. 收藏
    favs = c.favorites()
    print(f"[5] favorites: {len(favs)} 项")
    for s in favs[:5]:
        print(f"     {s.dentry_type or s.raw.get('resourceType','?'):8} {s.name[:48]}")
    ok.append(True)

    # 6. 最近访问
    rec = c.recent(page_size=10)
    print(f"[6] recent: {len(rec)} 项")
    for r in rec[:5]:
        print(f"     {(r.resource.name if r.resource else '-')[:48]:50} via {r.location[:24]}")
    ok.append(True)

    # 7. 下载: 优先真实二进制文件(xlsx/docx/pdf/ppt, 在线文档 .adoc 走不了预签名下载)
    BINARY_EXT = {"xlsx", "xls", "docx", "doc", "pdf", "ppt", "pptx", "zip", "png", "jpg"}
    candidates = [f for f in page.children if f.dentry_type == "file"
                  and (f.file_size or 0) > 0 and f.extension.lower() in BINARY_EXT]
    candidates += [f for f in favs if f.dentry_type == "file"
                   and (f.file_size or 0) > 0 and f.extension.lower() in BINARY_EXT]
    if not candidates:
        for r in rec:
            rs = r.resource
            if rs and rs.dentry_type == "file" and (rs.file_size or 0) > 0 \
                    and rs.extension.lower() in BINARY_EXT:
                candidates.append(rs)
    if candidates:
        target = min(candidates, key=lambda f: f.file_size or 0)
        t0 = time.time()
        path = c.download(target.dentry_uuid, dest_dir=OUT)
        dt = time.time() - t0
        size = path.stat().st_size
        print(f"[7] download: {target.name[:40]} ({fmt_size(size)}, {dt:.1f}s) -> {path}")
        ok.append(size > 0)
    else:
        print("[7] download: 无可下载文件, 跳过")

    passed = sum(1 for x in ok if x)
    print(f"\nRESULT: {passed}/{len(ok)} steps passed")
    return 0 if passed == len(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
