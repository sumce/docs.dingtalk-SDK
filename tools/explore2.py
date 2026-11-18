#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explore2.py — 关键端点报文结构与 /csp 协议探查。"""
import json, sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

items = [json.loads(l) for l in Path("analysis/items.jsonl").open(encoding="utf-8")]

# 0) /csp 到底在哪个 host, 用的什么协议
print("== /csp 相关 host/协议 ==")
for r in items:
    if "/csp" in r["path"]:
        print(f"  {r['host']:24} {r['method']} {r['path'][:60]} status={r['status']} req={r['req_body_bytes']}B resp={r['resp_body_bytes']}B mime={r['mimetype']}")

# WebSocket(101) 条目
print("\n== WebSocket 升级(101) ==")
for r in items:
    if r["status"] == 101:
        print(f"  {r['host']} {r['path'][:80]}")
        rh = r.get("resp_headers", {})
        print("   resp-headers:", {k: v for k, v in rh.items() if k in ("upgrade","connection","sec-websocket-protocol")})
        qh = {k: v for k, v in r.get("req_headers", {}).items() if k in ("upgrade","connection","sec-websocket-protocol","sec-websocket-key")}
        print("   req-headers:", qh)

# 1) 信封结构: 找出 resp_body 顶层键
env = Counter()
for r in items:
    rb = r.get("resp_body")
    if isinstance(rb, dict):
        env[tuple(sorted(rb.keys()))] += 1
print("\n== 响应信封顶层键组合 ==")
for keys, c in env.most_common(12):
    print(f"  {c:>4}  {keys}")

# 2) 关键端点详情: URL(含query) + 响应 data 顶层结构
KEY = ["recent/list", "dentry/list", "dentry/info", "share/listv2", "share/member/list",
       "token/getAccessToken", "resource/share_url", "resource/menus", "comment/list",
       "file/download", "dentry/operationGuard", "mine/orgs", "mine/info", "user/setting",
       "dentry/path", "dentryuuid/exchangeid", "dentry/launch", "star/list", "star/get",
       "space/list", "space/info", "doc/switches", "users/getUserInfo", "msgbox/badge",
       "unreadChatFile", "task/mine/recent"]
seen = set()
print("\n== 关键端点样本 ==")
for r in items:
    t = r["template"]
    hit = next((k for k in KEY if k in t), None)
    if not hit or hit in seen or r["status"] != 200:
        continue
    seen.add(hit)
    u = urlsplit(r["url"])
    print(f"\n--- {r['method']} {u.netloc}{u.path}")
    if u.query:
        print(f"    query: {u.query[:400]}")
    rb = r.get("req_body")
    if isinstance(rb, (dict, list)):
        print(f"    req : {json.dumps(rb, ensure_ascii=False)[:400]}")
    resp = r.get("resp_body")
    if isinstance(resp, dict):
        data = resp.get("data", resp.get("result", resp))
        if isinstance(data, dict):
            print(f"    resp.data keys: {list(data.keys())[:30]}")
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            print(f"    resp.data[{len(data)}] item keys: {list(data[0].keys())[:30]}")
            print(f"    resp.data[0]: {json.dumps(data[0], ensure_ascii=False)[:500]}")
        else:
            print(f"    resp.data: {json.dumps(data, ensure_ascii=False)[:300]}")
    elif isinstance(resp, str):
        print(f"    resp(text): {resp[:200]}")
