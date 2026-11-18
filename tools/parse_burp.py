#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_burp.py — 分片(流式)解析 Burp Suite 导出的 burp.xml

设计要点
========
1. 分片读取: `xml.etree.ElementTree.iterparse` 底层是 expat 增量解析器,
   按缓冲块喂数据; 每解析完一个 <item> 立即回调并 root.clear() 释放内存,
   峰值内存 ~= 单个 item, 与源文件大小无关(GB 级文件同样可跑)。
2. 报文解码: request/response 为 base64(CDATA); 按 base64 属性还原原始
   HTTP 字节流, 拆分 head/body, 响应体按 Content-Encoding 解压(gzip/deflate)。
3. 归一化: 输出 items.jsonl —— 每行一个 dict(method/url/query/headers/body/...)。
4. 聚合统计: 按 host、HTTP 方法、状态码、mimetype 统计;
   路径模板化(把 uuid/长数字段替换成 {id})后按 (method, 模板) 聚合端点,
   并对 JSON 报文提取 schema 概要 —— 用于反推 SDK 的 API 面。

用法
====
  python -I tools/parse_burp.py burp.xml --out-dir analysis [--body-cap 65536]
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import re
import sys
import zlib
from collections import Counter, defaultdict
from email.parser import BytesHeaderParser
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------- 工具函数

# 路径段模板化规则: 纯数字 / 8位以上hex / 16位以上大小写混杂base64样 / UUID / ding前缀长串
RE_PURE_NUM = re.compile(r"^\d+$")
RE_HEX = re.compile(r"^[0-9a-fA-F]{8,}$", re.IGNORECASE)
RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", re.IGNORECASE)
RE_B64ISH = re.compile(r"^[A-Za-z0-9+/_=~-]{16,}$")

def templatize(path: str) -> str:
    """把路径里的具体 ID 归一化为 {id}, 使同类端点可聚合。"""
    segs = []
    for seg in unquote(path).split("/"):
        if not seg:
            continue
        if (RE_PURE_NUM.match(seg) or RE_HEX.match(seg) or RE_UUID.match(seg)
                or (RE_B64ISH.match(seg) and not seg.isalpha() and not seg.islower())):
            segs.append("{id}")
        else:
            segs.append(seg)
    return "/" + "/".join(segs)

def decode_b64_field(elem: ET.Element) -> bytes:
    """按 base64 属性还原 CDATA 里的原始字节。"""
    raw = elem.text or ""
    if (elem.get("base64") or "false").lower() == "true":
        try:
            return base64.b64decode(raw)
        except Exception:
            return raw.encode("utf-8", "replace")
    return raw.encode("utf-8", "replace")

def split_http(raw: bytes):
    """拆 HTTP 报文: 返回 (首行, headers dict, body bytes)"""
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:  # 兼容仅 \n 的导出
        sep_lf = raw.find(b"\n\n")
        if sep_lf == -1:
            head, body = raw, b""
        else:
            head, body = raw[:sep_lf], raw[sep_lf + 2:]
    else:
        head, body = raw[:sep], raw[sep + 4:]
    lines = head.decode("latin-1", "replace").splitlines()
    first = lines[0] if lines else ""
    # 用 email 解析器折叠多值 header(Occasionally: Set-Cookie 多条)
    try:
        msg = BytesHeaderParser().parsebytes(head.split(b"\n", 1)[1] if b"\n" in head else b"")
        headers = {}
        for k, v in msg.items():
            headers.setdefault(k.lower(), []).append(v)
    except Exception:
        headers = {}
    return first, headers, body

def unwrap_body(body: bytes, headers: dict) -> bytes:
    """按 Content-Encoding 解压响应体。"""
    enc = (headers.get("content-encoding") or [""])[0].lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if "br" in enc:  # brotli 仅在装了 brotli 包时可用
            import brotli  # type: ignore
            return brotli.decompress(body)
    except Exception:
        pass
    return body

TEXTY_TYPES = ("json", "javascript", "html", "xml", "text", "x-www-form-urlencoded", "protobuf+json")

def sniff_body(body: bytes, content_type: str, cap: int):
    """按 content-type 尝试解析成结构化数据(json/表单), 否则留截断文本。"""
    if not body:
        return None
    ct = (content_type or "").lower()
    is_texty = any(t in ct for t in TEXTY_TYPES) or ct == ""
    data = body[:cap]
    if "json" in ct or (not ct and body[:1] in (b"{", b"[")):
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except Exception:
            pass
    if "x-www-form-urlencoded" in ct:
        try:
            return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        except Exception:
            pass
    if is_texty:
        try:
            text = data.decode("utf-8")
            return text if len(body) <= cap else text + f"...[截断, 全长{len(body)}B]"
        except UnicodeDecodeError:
            pass
    return f"<binary {len(body)}B {ct or 'unknown'}>"

def cookie_names(cookie_header: str) -> list[str]:
    """只取 Cookie 的键名(值脱敏)。"""
    return [k.strip() for k, _ in
            (c.split("=", 1) for c in cookie_header.split(";")) if k.strip()]

REDACT_KEYS = {"token", "access_token", "refresh_token", "authorization", "cookie",
               "set-cookie", "csrf", "password", "secret", "session", "code"}

def redact(obj):
    """递归脱敏, 防止凭证进入报告文件。"""
    if isinstance(obj, dict):
        return {k: ("***" if isinstance(k, str) and any(r in k.lower() for r in REDACT_KEYS)
                    else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj

# ---------------------------------------------------------------- 主体解析

ITEM_FIELDS = ("time", "url", "host", "port", "protocol", "method", "path",
               "extension", "request", "status", "responselength",
               "mimetype", "response", "comment")

def iter_items(xml_path: Path):
    """分片迭代 <item>: expat 增量喂入 + 处理完立即 clear, O(1) 内存。"""
    context = ET.iterparse(str(xml_path), events=("start", "end"))
    _, root = next(context)                    # 根节点 <items>
    meta = {"burpVersion": root.get("burpVersion", ""), "exportTime": root.get("exportTime", "")}
    n = 0
    for event, elem in context:
        if event == "end" and elem.tag == "item":
            yield meta, elem
            n += 1
            elem.clear()                        # 释放当前 item
            if n % 200 == 0:
                root.clear()                    # 周期性清根, 防碎片累积

def normalize_item(item: ET.Element, body_cap: int) -> dict:
    """单个 <item> → 归一化 dict。"""
    node = {c.tag: c for c in item if c.tag in ITEM_FIELDS}
    get = lambda t: (node[t].text or "") if t in node else ""

    url = get("url")
    sp = urlsplit(url)
    query = [{"name": k, "value": v} for k, v in parse_qsl(sp.query, keep_blank_values=True)]

    req_first, req_headers, req_body = split_http(decode_b64_field(node["request"])) if "request" in node else ("", {}, b"")
    rec = {
        "time": get("time"),
        "method": get("method") or (req_first.split(" ")[0] if req_first else "?"),
        "url": url,
        "host": sp.netloc or get("host"),
        "path": sp.path or get("path"),
        "template": f"{get('method')} {templatize(sp.path)}",
        "query": query,
        "status": int(get("status") or 0),
        "resp_len": int(get("responselength") or 0),
        "mimetype": get("mimetype"),
        "req_headers": {k: v[0] if len(v) == 1 else v for k, v in req_headers.items()},
        "req_cookie_names": cookie_names(req_headers.get("cookie", [""])[0]) if "cookie" in req_headers else [],
        "req_body_type": (req_headers.get("content-type") or [""])[0].split(";")[0] or None,
        "req_body": sniff_body(req_body, (req_headers.get("content-type") or [""])[0], body_cap),
        "req_body_bytes": len(req_body),
    }
    if "response" in node:
        _, resp_headers, resp_body = split_http(decode_b64_field(node["response"]))
        raw = unwrap_body(resp_body, resp_headers)
        rec.update({
            "resp_headers": {k: v[0] if len(v) == 1 else v for k, v in resp_headers.items()},
            "resp_body": sniff_body(raw, (resp_headers.get("content-type") or [""])[0], body_cap),
            "resp_body_bytes": len(raw),
        })
    else:
        rec.update({"resp_headers": {}, "resp_body": None, "resp_body_bytes": 0})
    return rec

# ---------------------------------------------------------------- 聚合统计

def flat_keys(obj, prefix="", depth=0, maxdepth=2):
    """JSON 结构概要: 递归收集 '路径: 类型', 控制深度。"""
    out = []
    if depth > maxdepth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(f"{prefix}{k}:{type(v).__name__}")
            out.extend(flat_keys(v, f"{prefix}{k}.", depth + 1, maxdepth))
    elif isinstance(obj, list):
        if obj:
            out.append(f"{prefix}[]:{len(obj)}")
            out.extend(flat_keys(obj[0], f"{prefix}[].", depth + 1, maxdepth))
    return out

def aggregate(records, out_dir: Path):
    by_host, by_method, by_status, by_mime = Counter(), Counter(), Counter(), Counter()
    endpoints = defaultdict(lambda: {"count": 0, "status": Counter(), "qs_keys": Counter(),
                                     "req_keys": Counter(), "resp_keys": Counter(),
                                     "req_body_types": Counter(), "samples": []})
    header_names, cookie_names_all, resp_header_names = Counter(), Counter(), Counter()
    for r in records:
        by_host[r["host"]] += 1
        by_method[r["method"]] += 1
        by_status[r["status"] or 0] += 1
        by_mime[r["mimetype"]] += 1
        header_names.update(r["req_headers"].keys())
        cookie_names_all.update(r["req_cookie_names"])
        resp_header_names.update(r["resp_headers"].keys())

        ep = endpoints[r["template"]]
        ep["count"] += 1
        ep["status"][r["status"] or 0] += 1
        ep["qs_keys"].update(q["name"] for q in r["query"])
        ep["req_body_types"][r["req_body_type"] or "∅"] += 1
        if isinstance(r["req_body"], (dict, list)):
            ep["req_keys"].update(flat_keys(r["req_body"], maxdepth=1))
        if isinstance(r["resp_body"], dict):
            ep["resp_keys"].update(flat_keys(r["resp_body"].get("data", r["resp_body"]), maxdepth=2))
        if len(ep["samples"]) < 3:
            ep["samples"].append({
                "url": r["url"], "status": r["status"],
                "req_body": r["req_body"] if isinstance(r["req_body"], (dict, list)) else None,
                "resp_data": (r["resp_body"].get("data") if isinstance(r["resp_body"], dict) else r["resp_body"]),
            })

    report = {
        "meta": {"source": "burp.xml", "tool": "parse_burp.py"},
        "totals": {"items": sum(by_host.values())},
        "by_host": by_host.most_common(),
        "by_method": by_method.most_common(),
        "by_status": by_status.most_common(),
        "by_mimetype": by_mime.most_common(),
        "request_header_names": header_names.most_common(),
        "response_header_names": resp_header_names.most_common(),
        "cookie_names": cookie_names_all.most_common(),
        "endpoints": {k: {**v, "status": dict(v["status"]),
                          "qs_keys": v["qs_keys"].most_common(20),
                          "req_keys": v["req_keys"].most_common(25),
                          "resp_keys": v["resp_keys"].most_common(30),
                          "req_body_types": v["req_body_types"].most_common()}
                      for k, v in sorted(endpoints.items(), key=lambda kv: -kv[1]["count"])},
    }
    return report

# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="分片解析 Burp XML 导出")
    ap.add_argument("xml", type=Path, help="burp.xml 路径")
    ap.add_argument("--out-dir", type=Path, default=Path("analysis"))
    ap.add_argument("--body-cap", type=int, default=65536, help="JSONL 中单个 body 的最大字节数")
    args = ap.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = args.out_dir / "items.jsonl"

    records = []
    with jsonl.open("w", encoding="utf-8") as fh:
        for meta, item in iter_items(args.xml):
            try:
                rec = normalize_item(item, args.body_cap)
            except Exception as e:                      # 单条损坏不拖垮整体
                rec = {"error": f"normalize failed: {e!r}", "url": ""}
            records.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    report = aggregate(records, args.out_dir)
    (args.out_dir / "report.json").write_text(
        json.dumps(redact(report), ensure_ascii=False, indent=2), encoding="utf-8")

    # Windows GBK 控制台兜底(配合 -I 时环境变量不生效)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # 控制台摘要
    p = print
    p(f"✔ items: {report['totals']['items']}  ->  {jsonl}")
    p("by_host:", report["by_host"][:8])
    p("by_method:", report["by_method"])
    p("by_status:", report["by_status"])
    p("top_endpoints:")
    for k, v in list(report["endpoints"].items())[:25]:
        p(f"  {v['count']:>4}x  {k}   status={v['status']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
