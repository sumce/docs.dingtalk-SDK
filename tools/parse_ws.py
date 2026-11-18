#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parse_ws.py — 分片解析 Burp 的 websocket_history.xml 并聚合协同协议。

导出格式: <websocket-history><message><direction>C-S|S-C</direction>
          <timestamp/><content/><url/></message>...

注意: Burp 该导出并不合规 —— <content> 内的裸 `&` 未转义(如预览 URL),
标准 XML 解析器会报 not well-formed。因此这里不按 XML 解析,
而是逐行流式提取(该导出每个字段独占一行, content 为单行 JSON),
既容错又保持 O(1) 内存。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def iter_messages(xml_path: Path):
    """逐行流式解析, 每 500 条后丢弃已处理文本(行迭代本身即分片)。"""
    buf: dict | None = None
    with xml_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line == "<message>":
                buf = {}
                continue
            if buf is None:
                continue
            if line == "</message>":
                if buf:
                    yield buf
                buf = None
                continue
            for tag in ("direction", "timestamp", "content", "url"):
                p = f"<{tag}>"
                if line.startswith(p) and line.endswith(f"</{tag}>"):
                    # Burp 对部分字段做了 XML 转义, 统一还原
                    buf[tag] = unescape(line[len(p):-len(tag) - 3])
                    break


def summarize_content(content: str):
    """提取消息的路由键: path / lwp / op 类型等。"""
    try:
        body = json.loads(content)
    except Exception:
        return "<non-json>", None
    if not isinstance(body, dict):
        return "<non-dict>", body
    route = body.get("path") or body.get("lwp") or ("response(code:%s)" % body.get("code"))
    return route, body


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "websocket_history.xml")
    out_dir = Path("analysis")
    out_dir.mkdir(exist_ok=True)

    per_url = Counter()
    per_route = defaultdict(lambda: Counter())          # route -> direction counts
    route_samples = defaultdict(list)                   # route -> sample payloads
    route_keys = defaultdict(Counter)                   # route -> top-level key combos
    payload_shapes = defaultdict(Counter)               # route -> payload 键形状
    mids = Counter()
    first_ts = last_ts = None
    jsonl = out_dir / "ws_items.jsonl"

    with jsonl.open("w", encoding="utf-8") as fh:
        for m in iter_messages(src):
            u = urlsplit(m["url"])
            url_key = f"{u.netloc}{u.path}"
            per_url[url_key] += 1
            route, body = summarize_content(m["content"])
            d = m["direction"]
            per_route[url_key][f"{d} {route}"] += 1
            if isinstance(body, dict):
                route_keys[url_key][tuple(sorted(body.keys()))] += 1
                payload = body.get("payload")
                if isinstance(payload, dict):
                    payload_shapes[url_key][tuple(sorted(payload.keys()))] += 1
                h = body.get("headers") or {}
                if isinstance(h, dict) and h.get("mid"):
                    mids[str(h["mid"])[:12]] += 1
            if len(route_samples[url_key]) < 4:
                route_samples[url_key].append({
                    "dir": d, "ts": m["timestamp"], "route": route,
                    "content": body if not isinstance(body, str) else body[:200],
                })
            fh.write(json.dumps({
                "url": url_key, "query": dict(parse_qs(u.query)), "direction": d,
                "timestamp": m["timestamp"], "route": route, "content": body,
            }, ensure_ascii=False, default=str) + "\n")
            ts = m["timestamp"]
            first_ts = first_ts or ts
            last_ts = ts

    print(f"✔ messages: {sum(per_url.values())}  ({first_ts} ~ {last_ts})  -> {jsonl}\n")
    print("== 通道 ==")
    for url, c in per_url.most_common():
        print(f"  {c:>6}  {url}")
    print("\n== 路由分布 (每通道) ==")
    for url, cnt in per_route.items():
        print(f"\n  [{url}]")
        for k, c in cnt.most_common(25):
            print(f"    {c:>6}  {k}")
    print("\n== payload 顶层键形状 (协同通道) ==")
    for url, shapes in payload_shapes.items():
        if "collab" in url or "editing" in url:
            print(f"\n  [{url}]")
            for keys, c in shapes.most_common(20):
                print(f"    {c:>6}  {keys}")
    # 样本文件
    samples = {url: v for url, v in route_samples.items()}
    (out_dir / "ws_report.json").write_text(
        json.dumps({
            "channels": per_url.most_common(),
            "routes": {u: c.most_common() for u, c in per_route.items()},
            "payload_shapes": {u: [list(k) for k, _ in c.most_common(20)] for u, c in payload_shapes.items()},
            "samples": samples,
        }, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    print(f"\n✔ 明细: {jsonl}  汇总: {out_dir / 'ws_report.json'}")


if __name__ == "__main__":
    main()
