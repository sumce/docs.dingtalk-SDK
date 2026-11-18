#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_test_edit.py — 真实在线编辑: S2~S10 每秒一格写入 SDK测试1~9。

⚠ 会写入真实群文档, 内容为可识别测试值, 可用 --clear 清空还原。
"""
import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ddocs import DdocsClient
from ddocs.collab import SheetCollabSession, open_sheet

TARGET_NAME = "20260702施工点计划及人员分配-中电二.xlsx"
SHEET_ID = "s1"
COL_S = 18          # 0 基: A=0 ... S=18
ROWS = list(range(1, 10))   # S2..S10 → 0 基行 1..9


def find_target(c: DdocsClient):
    for f in c.favorites() + [
        r.resource for r in c.recent(20) if r.resource
    ]:
        if f.name == TARGET_NAME:
            return f
    return None


async def run(clear: bool) -> int:
    c = DdocsClient()
    t = find_target(c)
    if not t:
        print(f"FAIL: 未找到目标文档 {TARGET_NAME}")
        return 1
    d = c.get_dentry(t.dentry_uuid)
    print(f"目标: {t.name}")
    print(f"docKey={d.doc_key}")

    s = SheetCollabSession(c, doc_key=d.doc_key or d.dentry_key,
                           heartbeat_interval=8, timeout=50)
    await s.connect()
    print(f"[OK] 建会话 awpConnectId={s.awp_connect_id}")

    ver = await asyncio.wait_for(s.ping_version(), timeout=55)
    print(f"[OK] 服务端版本引导: version={ver}")

    t0 = asyncio.get_event_loop().time()
    for i, row in enumerate(ROWS, start=1):
        value = "" if clear else f"SDK测试{i}"
        # selection 与 fillRange 同为 0 基, 指向同一单元格
        await asyncio.wait_for(s.send_selection(SHEET_ID, row=row, col=COL_S), timeout=5)
        v = await asyncio.wait_for(
            s.fill_cell(SHEET_ID, row=row, col=COL_S, value=value), timeout=15)
        print(f"  S{row + 1:<3} <- '{value}'  ack version={v}  ({asyncio.get_event_loop().time() - t0:.1f}s)")
        await asyncio.sleep(1.0)   # 一秒一格

    await s.close()
    print(f"\nEDIT_TEST: {'CLEARED' if clear else 'DONE'} (S2..S10, 共 {len(ROWS)} 格)")
    return 0


if __name__ == "__main__":
    clear = "--clear" in sys.argv
    sys.exit(asyncio.run(asyncio.wait_for(run(clear), timeout=120)))
