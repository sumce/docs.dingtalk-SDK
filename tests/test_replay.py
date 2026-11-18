"""delta 重放器单元测试 — 全部用抓包同构样本。"""

from __future__ import annotations

import json

from ddocs.replay import replay_deltas


def _d(version: int, ops: list[dict]) -> dict:
    return {"version": version, "operations": json.dumps(ops, ensure_ascii=False)}


def test_replay_fill_and_clear():
    deltas = [
        _d(1, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["s1", 0, 0, 1, 1],
            "cell": {"value": "Name", "formula": None}}}]),
        _d(2, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["s1", 0, 1, 1, 1],
            "cell": {"value": None, "formula": "=1+1"}}}]),
        _d(3, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "clear", "range": ["s1", 0, 1, 1, 1],
            "options": {}, "skipRows": []}}]),
    ]
    st = replay_deltas(deltas, doc_key="K")
    cells = st.read_cells("s1")["s1"]
    assert cells["r0c0"] == {"value": "Name", "formula": None}
    assert "r0c1" not in cells          # 已被 clear
    assert st.max_version == 3


def test_replay_sheet_lifecycle_and_dimension_shift():
    deltas = [
        # 建表并命名(客户端生成 id)
        _d(1, [{"v": 1, "action": "addSheet", "payload": {"meta": {"id": "st-abc"}}}]),
        _d(2, [{"v": 1, "action": "updateSheet",
                "payload": {"id": "st-abc", "meta": {"title": "111"}}}]),
        # 写两行
        _d(3, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["st-abc", 0, 0, 1, 1],
            "cell": {"value": "r0", "formula": None}}}]),
        _d(4, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["st-abc", 5, 0, 1, 1],
            "cell": {"value": "r5", "formula": None}}}]),
        # 在第 2 行(0基 from=2)前插 3 行 → r5 平移到 r8
        _d(5, [{"v": 1, "action": "insertDimension", "payload": {
            "section": {"sheetId": "st-abc", "isRow": True, "from": 2, "count": 3}}}]),
        # 删除第 0 行 → "r0" 被删, r8 回到 r7
        _d(6, [{"v": 1, "action": "deleteDimension", "payload": {
            "section": {"sheetId": "st-abc", "isRow": True, "from": 0, "count": 1}}}]),
    ]
    st = replay_deltas(deltas, doc_key="K")
    titles = {t["id"]: t["title"] for t in st.sheet_titles()}
    assert titles["st-abc"] == "111"
    cells = st.read_cells("st-abc")["st-abc"]
    assert "r0c0" not in cells                       # 被删除
    assert cells["r7c0"]["value"] == "r5"            # 5+3-1=7


def test_replay_style_tracking():
    """fillBy=style 合并进单元格样式(槽3=背景色, 抓包标定)。"""
    deltas = [
        _d(1, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["s1", 0, 0, 1, 1],
            "cell": {"value": "红字", "formula": None}}}]),
        _d(2, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "style", "range": ["s1", 0, 0, 2, 2],
            "style": {"3": "#C10002"}}}]),
        _d(3, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "style", "range": ["s1", 0, 0, 1, 1],
            "style": {"8": "#FFFFFF"}}}]),   # 叠加字体色
    ]
    st = replay_deltas(deltas)
    cells = st.read_cells("s1")["s1"]
    assert cells["r0c0"]["style"] == {"3": "#C10002", "8": "#FFFFFF"}   # 合并
    assert cells["r1c1"]["style"] == {"3": "#C10002"}                    # 只吃背景
    assert cells["r0c0"]["value"] == "红字"


def test_replay_orders_by_version_and_ignores_unknown():
    deltas = [
        _d(5, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["s1", 0, 0, 1, 1],
            "cell": {"value": "final", "formula": None}}}]),
        _d(2, [{"v": 1, "action": "fillRange", "payload": {
            "fillBy": "cell", "range": ["s1", 0, 0, 1, 1],
            "cell": {"value": "old", "formula": None}}}]),
        _d(3, [{"v": 1, "action": "futureAction", "payload": {}}]),
    ]
    st = replay_deltas(deltas)
    assert st.read_cells("s1")["s1"]["r0c0"]["value"] == "final"   # 版本序生效
