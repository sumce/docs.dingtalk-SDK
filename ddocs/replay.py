"""delta 重放器 — 从 getDeltasWithVersion 的操作流重建单元格状态。

依据真实抓包(2026-11-17): collab.dingtalk.com/api/collab/v2/getDeltasWithVersion
?version=0&limit=500 (请求头 a-doc-key) 返回明文 OT 操作流, 逐条重放即得
单元格内容/sheet 元数据 —— 无需触碰端到端加密的 skeleton 分片。

支持的 action(全部来自真实抓包):
  fillRange(cell)     写值/公式
  fillRange(clear)    清空矩形
  addSheet            新建工作表(meta.id, 客户端生成)
  updateSheet         工作表改名(meta.title)
  insertDimension / deleteDimension   行列插入/删除(带坐标位移)
  mirrorRange         粘贴(样式复杂, 仅记录占位不重建值)
  其余 action 忽略并计数。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Optional


class SheetState:
    def __init__(self, sheet_id: str):
        self.sheet_id = sheet_id
        self.title: Optional[str] = None       # None=未观测到命名(来自 skeleton)
        self.cells: dict[tuple[int, int], dict] = {}   # (row,col) -> {"value","formula"}
        self.styles: dict[tuple[int, int], dict] = {}  # (row,col) -> 合并后的样式槽位字典
        self.ignored_actions: list[str] = []

    def set_cell(self, row: int, col: int, value: Any, formula: Optional[str]) -> None:
        self.cells[(row, col)] = {"value": value, "formula": formula}

    def apply_style(self, row: int, col: int, rows: int, cols: int, style: dict) -> None:
        """对矩形区域内每个单元格合并内联样式(抓包 fillBy=style 语义)。"""
        for r in range(row, row + max(rows, 0)):
            for c in range(col, col + max(cols, 0)):
                cur = self.styles.get((r, c)) or {}
                cur.update(style)
                self.styles[(r, c)] = cur

    def clear_rect(self, row: int, col: int, rows: int, cols: int) -> None:
        for r in range(row, row + max(rows, 0)):
            for c in range(col, col + max(cols, 0)):
                self.cells.pop((r, c), None)

    def shift_dimension(self, is_row: bool, frm: int, count: int, insert: bool) -> None:
        """insert/deleteDimension 的坐标位移。"""
        if count <= 0:
            return
        idx = 0 if is_row else 1
        new_cells: dict[tuple[int, int], dict] = {}
        for (r, c), cell in self.cells.items():
            pos = (r, c)[idx]
            if insert:
                npos = pos + count if pos >= frm else pos
            else:
                if frm <= pos < frm + count:
                    continue                      # 被删除
                npos = pos - count if pos >= frm + count else pos
            key = (npos, c) if is_row else (r, npos)
            new_cells[key] = cell
        self.cells = new_cells
        new_styles: dict[tuple[int, int], dict] = {}
        for (r, c), style in self.styles.items():
            pos = (r, c)[idx]
            if not insert and frm <= pos < frm + count:
                continue
            if insert:
                npos = pos + count if pos >= frm else pos
            else:
                npos = pos - count if pos >= frm + count else pos
            key = (npos, c) if is_row else (r, npos)
            new_styles[key] = style
        self.styles = new_styles


class WorkbookState:
    def __init__(self):
        self.sheets: dict[str, SheetState] = defaultdict(lambda: SheetState(""))
        self.doc_key: str = ""
        self.max_version: int = 0

    def sheet(self, sheet_id: str) -> SheetState:
        st = self.sheets[sheet_id]
        st.sheet_id = sheet_id
        return st

    def sheet_titles(self) -> list[dict]:
        """[{id, title, cells}] — title 未观测到时为 None。"""
        return [{"id": sid, "title": st.title, "cells": len(st.cells)}
                for sid, st in self.sheets.items()]

    def read_cells(self, sheet_id: Optional[str] = None, *,
                   with_style: bool = True) -> dict[str, dict]:
        """读取单元格: 指定 sheetId 或全部。

        返回 {sheetId: {"r行c列": {"value", "formula"[, "style"]}}}，
        style 为合并后的样式槽位字典(如 {"3": "#C10002"} 背景色)。
        """
        out = {}
        ids = [sheet_id] if sheet_id else sorted(self.sheets)
        for sid in ids:
            st = self.sheets.get(sid)
            if st is None:
                continue
            entries = {}
            keys = set(st.cells) | (set(st.styles) if with_style else set())
            for (r, c) in sorted(keys):
                cell = dict(st.cells.get((r, c)) or {})
                if with_style:
                    style = st.styles.get((r, c))
                    if style:
                        cell["style"] = style
                entries[f"r{r}c{c}"] = cell
            out[sid] = entries
        return out


def _ops_of(entry: dict) -> list[dict]:
    ops = entry.get("operations")
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except json.JSONDecodeError:
            return []
    return ops if isinstance(ops, list) else []


def replay_deltas(deltas: list[dict], doc_key: str = "") -> WorkbookState:
    """按版本序重放 delta 流 → 工作簿状态。"""
    state = WorkbookState()
    state.doc_key = doc_key
    for entry in sorted(deltas, key=lambda e: e.get("version") or 0):
        state.max_version = max(state.max_version, entry.get("version") or 0)
        for op in _ops_of(entry):
            action = op.get("action")
            p = op.get("payload") or {}

            if action == "fillRange":
                rng = p.get("range") or []
                if len(rng) < 5:
                    continue
                sid, row, col, rows, cols = rng[0], rng[1], rng[2], rng[3], rng[4]
                sh = state.sheet(sid)
                fill_by = p.get("fillBy")
                if fill_by == "clear":
                    sh.clear_rect(row, col, rows, cols)
                elif fill_by in ("cell", "style", "border"):
                    if fill_by == "cell":
                        cell = p.get("cell") or {}
                        sh.set_cell(row, col, cell.get("value"), cell.get("formula"))
                    elif fill_by == "style":
                        sh.apply_style(row, col, rows, cols, p.get("style") or {})
                    # border 不改值
                # 其他 fillBy(如 group)忽略

            elif action == "addSheet":
                sid = ((p.get("meta") or {}).get("id")) or ""
                if sid:
                    state.sheet(sid)

            elif action == "updateSheet":
                sid = p.get("id") or ""
                title = (p.get("meta") or {}).get("title")
                if sid and title is not None:
                    state.sheet(sid).title = title

            elif action in ("insertDimension", "deleteDimension"):
                sec = p.get("section") or {}
                state.sheet(sec.get("sheetId") or "").shift_dimension(
                    bool(sec.get("isRow")), sec.get("from") or 0,
                    sec.get("count") or 0, action == "insertDimension")

            elif action == "mirrorRange":
                # 粘贴: 值语义复杂(含样式切片), 记录为忽略
                state.sheet((p.get("to") or ["?"])[0]).ignored_actions.append("mirrorRange")

            else:
                pass  # 未知 action 忽略

    return state
