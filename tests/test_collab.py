"""协同编辑模块离线测试 — 进程内假 collab 服务端, 按抓包应答回放。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

websockets = pytest.importorskip("websockets")

from ddocs.client import DdocsClient
from ddocs.collab import SheetCollabSession, fill_cell_op, update_sheet_op, dimension_op


class FakeCollabServer:
    """按抓包形态应答: /create 确认 / /! 心跳带版本 / /s/delta 返回 ack+新版本。"""

    def __init__(self):
        self.received: list[dict] = []
        self.server = None
        self.port = 0

    async def _handler(self, ws):
        self.ws = ws   # 保存连接, 供测试模拟服务端主动推送
        async for raw in ws:
            frame = json.loads(raw)
            self.received.append(frame)
            path = frame["path"]
            if path == "/create":
                await ws.send(json.dumps({"path": "/create", "headers": {},
                                          "payload": {}, "status": 200}))
                await ws.send(json.dumps({"path": "/limit/edit",
                                          "payload": {"editStatus": "edit"}, "status": 200}))
            elif path == "/!":
                await ws.send(json.dumps({"path": "/!", "headers": {},
                                          "payload": {"version": 100}, "status": 200}))
            elif path == "/s/delta":
                payload = frame["payload"]
                ops = json.loads(payload["operations"])          # 抓包: operations 为 JSON 字符串
                assert isinstance(ops, list) and ops
                await ws.send(json.dumps({
                    "path": "/s/delta", "headers": {},
                    "payload": {"bornTime": 0, "otAlg": "js:1.0", "guid": payload["guid"],
                                "protocolVersion": 1, "type": "ack", "version": 101},
                    "status": 200,
                }))

    def start(self):
        async def _run():
            self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            await self.server.serve_forever()

        self.thread = threading.Thread(target=lambda: asyncio.run(_run()), daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.port:
                return
            time.sleep(0.05)
        raise RuntimeError("fake server 未启动")


def make_client() -> DdocsClient:
    c = DdocsClient(cred_file="unused.json")
    c.s.min_interval = 0.0
    c.s.get_access_token = lambda force=False: "fake-a-token"  # type: ignore[method-assign]
    return c


def test_fill_cell_op_wire_format():
    """与抓包逐字段对齐。"""
    op = fill_cell_op("s1", 5, 17, value="x")
    assert op == {"v": 1, "action": "fillRange",
                  "payload": {"fillBy": "cell", "range": ["s1", 5, 17, 1, 1],
                              "cell": {"value": "x", "formula": None}}}


def test_op_builders_wire_format():
    """新增操作构造器与抓包逐字段对齐。"""
    assert update_sheet_op("st-x", "111") == {
        "v": 1, "action": "updateSheet",
        "payload": {"id": "st-x", "meta": {"title": "111"}}}
    assert dimension_op("kgqie6hm", True, 2, 4, False) == {
        "v": 1, "action": "deleteDimension",
        "payload": {"section": {"sheetId": "kgqie6hm", "isRow": True,
                                 "from": 2, "count": 4}}}


def test_fill_rect_batched_single_delta():
    """矩形批量写入: 多格合并进一次 delta。"""
    srv = FakeCollabServer()
    srv.start()
    c = make_client()

    async def main():
        async with SheetCollabSession(c, doc_key="KEY-R", ws_url=f"ws://127.0.0.1:{srv.port}",
                                      heartbeat_interval=30) as s:
            v = await asyncio.wait_for(
                s.fill_rect("s1", 0, 0, [["a", "b"], ["=1+1", None]]), timeout=5)
            assert v == 101
            deltas = [f for f in srv.received if f["path"] == "/s/delta"]
            assert len(deltas) == 1                      # 一次 delta
            ops = json.loads(deltas[0]["payload"]["operations"])
            assert len(ops) == 3                          # None 被跳过
            assert ops[2]["payload"]["cell"]["formula"] == "=1+1"

    asyncio.run(asyncio.wait_for(main(), timeout=15))


def test_switch_and_observe_sheets():
    """切换工作表 = 对目标 sheet 广播 selection; sheetId 从服务端帧观测。"""
    srv = FakeCollabServer()
    srv.start()
    c = make_client()

    async def main():
        async with SheetCollabSession(c, doc_key="KEY-SW", ws_url=f"ws://127.0.0.1:{srv.port}",
                                      heartbeat_interval=30) as s:
            await asyncio.wait_for(s.switch_sheet("s3"), timeout=5)
            assert s.current_sheet == "s3"
            # 新建工作表(addSheet+updateSheet 一次 delta)与清空区域
            await asyncio.wait_for(s.add_sheet("SDK测试表"), timeout=5)
            await asyncio.wait_for(s.clear_range("s1", 1, 18, 9, 1), timeout=5)
            adds = [f for f in srv.received if f["path"] == "/s/delta"]
            all_ops = []
            for f in adds:
                ops = json.loads(f["payload"]["operations"])
                all_ops.extend(ops)
            actions = [o["action"] for o in all_ops]
            assert "addSheet" in actions and "updateSheet" in actions
            clear = next(o for o in all_ops if o["action"] == "fillRange"
                         and o["payload"].get("fillBy") == "clear")
            assert clear["payload"]["range"] == ["s1", 1, 18, 9, 1]
            upd = next(o for o in all_ops if o["action"] == "updateSheet")
            assert upd["payload"]["meta"]["title"] == "SDK测试表"
            # 服务端推: 他人 selection(s9) + checkpoint(含 extraPartsOssKey)
            await asyncio.sleep(0.1)
            await srv.ws.send(json.dumps({
                "path": "/s/selections",
                "payload": {"uid": 2, "uidStr": "u2",
                            "selection": {"sheetId": "s9", "row": 1, "col": 1}},
                "status": 200}))
            await srv.ws.send(json.dumps({
                "path": "/s/checkpoint",
                "payload": {"baseVersion": 5,
                            "extraPartsOssKey": {"s1": "k1", "s2": "k2", "st-x": "k3"}},
                "status": 200}))
            n = await s.drain(1.5)
            assert n >= 2
            assert set(s.list_sheet_ids()) >= {"s3", "s9", "s1", "s2", "st-x"}
            # 服务端收到的切换帧与抓包同构
            sel = next(f for f in srv.received
                       if f["path"] == "/s/selections" and f["payload"]["selection"]["sheetId"] == "s3")
            assert sel["payload"]["selection"]["deviceId"] == s.device_id

    asyncio.run(asyncio.wait_for(main(), timeout=15))


def test_collab_session_roundtrip():
    srv = FakeCollabServer()
    srv.start()
    c = make_client()

    async def main():
        async with SheetCollabSession(c, doc_key="Q35OfakeKEY", ws_url=f"ws://127.0.0.1:{srv.port}",
                                      heartbeat_interval=0.05) as s:
            # 编辑 → ack 版本
            v = await s.fill_cell("s1", row=5, col=17, value="hello")
            assert v == 101 and s.version == 101
            # 光标广播不阻塞
            await asyncio.wait_for(s.send_selection("s1", 5, 18), timeout=2)
            await asyncio.sleep(0.15)  # 让心跳跑一轮
            paths = [f["path"] for f in srv.received]
            assert "/create" in paths and "/s/selections" in paths and "/!" in paths
            # 建会话帧与抓包同构(docKey/multiChannelKey/logicChannelId)
            create = next(f for f in srv.received if f["path"] == "/create")
            h = create["headers"]
            assert h["docKey"] == h["multiChannelKey"] == "Q35OfakeKEY"
            assert h["awpConnectId"] == s.awp_connect_id
            delta = next(f for f in srv.received if f["path"] == "/s/delta")
            dp = delta["payload"]
            assert dp["contextVersion"] == 0 and dp["otAlg"] == "js:1.0"
            assert json.loads(dp["operations"])[0]["payload"]["range"] == ["s1", 5, 17, 1, 1]

    asyncio.run(asyncio.wait_for(main(), timeout=15))
