#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""draw_image.py — 把图片按像素用单元格背景色画到在线表格。

用法:
  python tools/draw_image.py <image> [cols]
示例:
  python tools/draw_image.py OIP-C.jpg 150
"""
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from ddocs import DdocsClient
from ddocs.collab import SheetCollabSession, open_sheet

UUID = 'l6Pm2Db8D4GDK0aqUygR3gyB8xLq0Ee4'      # TEST 文档
BATCH = 150                                     # 每批 delta 的 op 数
COL_W, ROW_H = 2.2, 15                          # 近似正方形像素


def otsu_threshold(hist: list[int], total: int) -> int:
    """Otsu 自动阈值(类间方差最大化)。"""
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = w_b = 0
    best, thr = 0.0, 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, thr = between, t
    return thr


def image_to_grid(path: Path, cols: int) -> tuple[list[list[Optional[str]]], str]:
    """图片 → 二值网格(深色=主题色, 浅色=None), 保持宽高比。"""
    img = Image.open(path).convert('L')
    rows = max(1, round(cols * img.height / img.width))
    img = img.resize((cols, rows), Image.LANCZOS)
    hist = img.histogram()
    thr = otsu_threshold(hist, cols * rows)
    dark_cut = max(0, min(int(thr * 0.55), 100))     # 深色: 主体笔画
    light_cut = min(255, thr + 20)                    # 浅色: 抗锯齿过渡
    px = img.load()
    grid: list[list[Optional[str]]] = []
    for y in range(rows):
        line = []
        for x in range(cols):
            v = px[x, y]
            line.append('#333333' if v < dark_cut else ('#C8C8C8' if v < light_cut else None))
        grid.append(line)
    for line in grid:   # ASCII 预览: 发送前确认字形
        print(''.join('#' if v == '#333333' else ('+' if v == '#C8C8C8' else ' ') for v in line))
    return grid, f'Otsu={thr} dark<{dark_cut} light<{light_cut}'


def rle_ops(sheet_id: str, grid) -> list[dict]:
    """逐行行程压缩 → fillRange style ops。"""
    ops = []
    for y, row in enumerate(grid):
        x, width = 0, len(row)
        while x < width:
            c = row[x]
            if c is None:
                x += 1
                continue
            x0 = x
            while x < width and row[x] == c:
                x += 1
            ops.append({'v': 1, 'action': 'fillRange', 'payload': {
                'fillBy': 'style', 'range': [sheet_id, y, x0, 1, x - x0],
                'style': {'3': c}}})
    return ops


async def main(img_path: str, cols: int) -> int:
    grid, info = image_to_grid(Path(img_path), cols)
    gh = len(grid)
    n_dark = sum(1 for r in grid for v in r if v == '#333333')
    print(f'网格 {cols}x{gh}, 阈值: {info}, 深色格 {n_dark}')

    ops = rle_ops('SHEET', grid)
    print(f'RLE 后 ops: {len(ops)}')

    c = DdocsClient()
    s = await open_sheet(c, UUID).connect()
    title = f'像素画 {Path(img_path).stem} {cols}x{gh}'
    sid = await s.add_sheet(title, index=1)
    print(f'[sheet] {sid}  {title!r}')

    t0 = time.time()
    ver = 0
    for i in range(0, len(ops), BATCH):
        batch = [{**o, 'payload': {**o['payload'],
                  'range': [sid] + o['payload']['range'][1:]}}
                 for o in ops[i:i + BATCH]]
        ver = await s._submit_delta(batch)
        print(f'  batch {i // BATCH + 1}: {len(batch)} ops -> v{ver}')
    await s.set_col_width(sid, 0, COL_W, cols)
    await s.set_row_height(sid, 0, ROW_H, gh)
    dt = time.time() - t0
    await s.close()
    print(f'[DONE] {dt:.1f}s  打开 TEST 文档 → 「{title}」查看')
    return 0


if __name__ == '__main__':
    img = sys.argv[1] if len(sys.argv) > 1 else 'OIP-C.jpg'
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    sys.exit(asyncio.run(asyncio.wait_for(main(img, n), timeout=600)))
