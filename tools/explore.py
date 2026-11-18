#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""explore.py — 基于 items.jsonl 深挖 docs.dingtalk.com 的 API 面。"""
import json, sys, re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

items = [json.loads(l) for l in Path("analysis/items.jsonl").open(encoding="utf-8")]
docs = [r for r in items if r.get("host") == "docs.dingtalk.com"]
print(f"docs.dingtalk.com items: {len(docs)}\n")

# 1) 按 API 前缀分组
groups = defaultdict(list)
for r in docs:
    parts = r["path"].strip("/").split("/")
    prefix = "/" + "/".join(parts[:3]) if len(parts) >= 3 else "/" + r["path"]
    groups[re.sub(r"/[^/]*(api|v\d).*", "", prefix) or prefix].append(r)

print("== API 前缀分组 (按请求数) ==")
for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    print(f"  {len(rs):>4}  {g}")

# 2) 全部端点模板清单(docs 域)
print("\n== 全部端点模板 ==")
for r in docs:
    print(f"  {r['method']:4} {r['status']:>3} {r['template']}")

# 3) 认证与关键请求头
hdr = Counter()
for r in docs:
    hdr.update(r["req_headers"].keys())
print("\n== 请求头(全部) ==")
for k, c in hdr.most_common():
    print(f"  {c:>4}  {k}")

# 4) Cookie 键名
ck = Counter()
for r in docs:
    ck.update(r.get("req_cookie_names", []))
print("\n== Cookie 键名 ==")
for k, c in ck.most_common():
    print(f"  {c:>4}  {k}")

# 5) /csp 协议探查
print("\n== /csp 样本 ==")
for r in docs:
    if r["path"].startswith("/csp"):
        print(json.dumps({k: r[k] for k in ("method","url","req_body_type","req_body_bytes","resp_body_bytes")}, ensure_ascii=False)[:400])
        rb = r.get("req_body")
        s = json.dumps(rb, ensure_ascii=False)[:600] if not isinstance(rb, str) else rb[:600]
        print("  req_body:", s)
        resp = r.get("resp_body")
        s2 = json.dumps(resp, ensure_ascii=False)[:600] if not isinstance(resp, str) else resp[:600]
        print("  resp_body:", s2)
        break
