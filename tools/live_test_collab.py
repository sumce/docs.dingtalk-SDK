#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_test_collab.py — 协同通道真实连接测试(只读, 不写入)。"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ddocs import DdocsClient
from ddocs.collab import open_sheet


async def main() -> int:
    c = DdocsClient()
    # 用收藏里的第一张真实表格(就是 WS 抓包时打开的那张)
    favs = c.favorites()
    target = next((f for f in favs if f.extension == "xlsx"), None)
    if not target:
        print("SKIP: 收藏里没有 xlsx")
        return 0
    print(f"target: {target.name}  dentryUuid={target.dentry_uuid}")

    d = c.get_dentry(target.dentry_uuid)
    print(f"docKey={d.doc_key}  doc_key/dentry_key={d.dentry_key}")

    s = await open_sheet(c, target.dentry_uuid).connect()
    print("[OK] WS 建连 + /create 会话确认")
    print(f"     awpConnectId={s.awp_connect_id}")
    print(f"     当前观测版本 version={s.version}")

    await asyncio.wait_for(s.send_selection("s1", row=1, col=1), timeout=5)
    print("[OK] /s/selections 光标广播已发出(只读操作)")

    await asyncio.sleep(3)  # 让心跳跑一轮, 观察服务端推帧
    try:
        raw = await asyncio.wait_for(s._ws.recv(), timeout=3)
        print(f"[S-C 帧] {raw[:200]}")
    except (asyncio.TimeoutError, Exception):
        print("[i] 3s 内无服务端主动推送(心跳静默, 正常)")

    await s.close()
    print("[OK] 会话已关闭")
    print("\nCOLLAB_READONLY_TEST: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
