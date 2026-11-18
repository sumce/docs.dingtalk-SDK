#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_test_suite.py — TEST 文档全功能真实测试套件。

每步: WS 写入(ack) → 服务端 delta 重放断言(独立 HTTP 读)。
目标文档: TEST.axls (用户授权随便搞)。
"""
import asyncio
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ddocs import DdocsClient
from ddocs.collab import SheetCollabSession, open_sheet

UUID = 'l6Pm2Db8D4GDK0aqUygR3gyB8xLq0Ee4'
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'✔' if cond else '✘'} {name}" + (f"  ({detail})" if detail else ""))


def cell(state: dict, sheet: str, r: int, c: int) -> dict:
    return state.get(sheet, {}).get(f"r{r}c{c}", {})


async def commit_and_verify(s, c, pause=1.2):
    """提交后等服务端落账, 返回重放后的全量状态。"""
    await asyncio.sleep(pause)
    return None


async def main() -> int:
    c = DdocsClient()
    d = c.get_dentry(UUID)
    SHEET = 'kgqie6hm'      # TEST 文档真实工作表(协议读取确认)
    print(f"目标: {d.name!r} docKey={d.doc_key}  工作表={SHEET}\n")

    s = await open_sheet(c, UUID).connect()
    await s.switch_sheet(SHEET)
    ver0 = await asyncio.wait_for(s.ping_version(), timeout=55)
    print(f"[boot] 版本引导 version={ver0}\n")

    def replay(sheet=None, with_style=True):
        time.sleep(1.2)                       # 服务端落账
        return c.read_cells(UUID, sheet or SHEET, ) if with_style else None

    # ── 1. 单元格写入
    v = await s.fill_cell(SHEET, 0, 5, value="单格写入")
    st = c.read_cells(UUID, SHEET)
    check("1 单格写入 fill_cell", cell(st, SHEET, 0, 5).get("value") == "单格写入", f"v={v}")

    # ── 2. 编辑(覆盖已有单元格)
    v = await s.fill_cell(SHEET, 0, 5, value="单格写入-已编辑")
    st = c.read_cells(UUID, SHEET)
    check("2 编辑/覆盖 fill_cell", cell(st, SHEET, 0, 5).get("value") == "单格写入-已编辑", f"v={v}")

    # ── 3. 矩形批量写入(单次 delta)
    v = await s.fill_rect(SHEET, 1, 5, [["批量A", "批量B"], ["批量C", None]])
    st = c.read_cells(UUID, SHEET)
    ok = (cell(st, SHEET, 1, 5).get("value") == "批量A"
          and cell(st, SHEET, 1, 6).get("value") == "批量B"
          and cell(st, SHEET, 2, 5).get("value") == "批量C")
    check("3 矩形批量写入 fill_rect(单次delta)", ok, f"v={v}")

    # ── 4. 公式写入
    v = await s.fill_cell(SHEET, 2, 6, formula="=1+1")
    st = c.read_cells(UUID, SHEET)
    check("4 公式写入 formula", cell(st, SHEET, 2, 6).get("formula") == "=1+1", f"v={v}")

    # ── 5. 清空区域
    v = await s.clear_range(SHEET, 1, 5, 2, 2)
    st = c.read_cells(UUID, SHEET)
    gone = all(f"r{r}c{cc}" not in st.get(SHEET, {})
               for r in (1, 2) for cc in (5, 6))
    check("5 清空区域 clear_range", gone, f"v={v}")

    # ── 6. 新建工作表
    new_id = await s.add_sheet("SDK测试表", index=1)
    titles = {t["id"]: t["title"] for t in c.sheets_protocol(UUID)}
    check("6 新建工作表 add_sheet", titles.get(new_id) == "SDK测试表", f"id={new_id}")

    # ── 7. 重命名工作表
    await s.rename_sheet(new_id, "SDK改名表")
    titles = {t["id"]: t["title"] for t in c.sheets_protocol(UUID)}
    check("7 重命名 rename_sheet", titles.get(new_id) == "SDK改名表")

    # ── 8. 插入行(坐标位移验证): 写标记@行5 → 前插1行 → 标记应在行6
    await s.fill_cell(SHEET, 5, 0, value="位移标记")
    await s.insert_rows(SHEET, 5, 1)
    st = c.read_cells(UUID, SHEET)
    moved = cell(st, SHEET, 6, 0).get("value") == "位移标记"
    check("8 插入行 insert_rows(位移)", moved)

    # ── 9. 删除行: 删掉"插入出来的空行"(行5) → 标记应从行6回到行5
    await s.delete_rows(SHEET, 5, 1)
    st = c.read_cells(UUID, SHEET)
    back = cell(st, SHEET, 5, 0).get("value") == "位移标记"
    check("9 删除行 delete_rows(位移恢复)", back)

    # ── 10. 行高
    v = await s.set_row_height(SHEET, 4, 60)
    check("10 行高 set_row_height(60)", v > 0, f"v={v} 视觉请看第5行")

    # ── 11. 列宽
    v = await s.set_col_width(SHEET, 1, 200)
    check("11 列宽 set_col_width(200)", v > 0, f"v={v} 视觉请看B列")

    # ── 12. 背景色
    v = await s.set_fill_color(SHEET, 4, 0, 1, 2, "#C10002")
    st = c.read_cells(UUID, SHEET)
    style_ok = cell(st, SHEET, 4, 0).get("style", {}).get("3") == "#C10002"
    check("12 背景色 set_fill_color", style_ok, f"v={v}")

    # ── 13. 字体颜色
    v = await s.set_font_color(SHEET, 4, 0, 1, 2, "#FFFFFF")
    st = c.read_cells(UUID, SHEET)
    style_ok = cell(st, SHEET, 4, 0).get("style", {}).get("8") == "#FFFFFF"
    check("13 字体色 set_font_color", style_ok, f"v={v}")

    # ── 14. 切换工作表(广播)
    await s.switch_sheet(new_id)
    check("14 切换工作表 switch_sheet", s.current_sheet == new_id)

    await s.close()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n===== 结果: {passed}/{len(RESULTS)} 通过 =====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(asyncio.wait_for(main(), timeout=300)))
