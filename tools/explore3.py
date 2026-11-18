#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explore3.py — 扫码登录链路与下载链路细节探查。"""
import json, sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

items = [json.loads(l) for l in Path("analysis/items.jsonl").open(encoding="utf-8")]

def show(r, body_cap=800):
    from urllib.parse import urlsplit
    u = urlsplit(r["url"])
    print(f"\n--- {r['method']} {u.netloc}{u.path}  [{r['status']}]")
    if u.query: print(f"    query: {u.query[:300]}")
    interesting = {k: v for k, v in r.get("req_headers", {}).items()
                   if k in ("content-type", "referer", "origin", "a-token", "x-xsrf-token", "corp-id")}
    if interesting: print(f"    req-h: {interesting}")
    rb = r.get("req_body")
    if rb is not None:
        s = json.dumps(rb, ensure_ascii=False) if not isinstance(rb, str) else rb
        print(f"    req-b: {s[:body_cap]}")
    setc = r.get("resp_headers", {}).get("set-cookie")
    if setc:
        names = [c.split("=", 1)[0] for c in (setc if isinstance(setc, list) else [setc])]
        print(f"    set-cookie: {names}")
    resp = r.get("resp_body")
    s = json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp
    print(f"    resp : {s[:body_cap]}")

print("=" * 30, "login.dingtalk.com 全部请求", "=" * 30)
for r in items:
    if "login.dingtalk.com" in r["host"]:
        show(r)

print("\n" + "=" * 30, "docs 登录态/会话", "=" * 30)
for r in items:
    if r["template"] in ("GET /i/api/login/status", "POST /api/users/getUserInfo",
                         "POST /portal/api/v1/token/getAccessToken"):
        show(r)

print("\n" + "=" * 30, "下载链路(file/download + operationGuard + OSS)", "=" * 30)
for r in items:
    if "file/download" in r["template"] or "operationGuard" in r["template"] or "downloadCount" in r["path"]:
        show(r)
