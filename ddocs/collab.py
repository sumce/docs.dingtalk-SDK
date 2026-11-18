"""协同编辑 — collab-ws.dingtalk.com/ws/editing/v3.2 的 OT 协议客户端。

协议还原自 websocket_history.xml 抓包(详见 docs/API_ANALYSIS.md §7):

    连接:  wss://collab-ws.dingtalk.com/ws/editing/v3.2?token=<a-token>&awpConnectId=<uuid>
    建会话: C-S {"path":"/create","headers":{"mid","awpConnectId","docKey","multiChannelKey",
              "logicChannelId"},"payload":{"sessionFeature":0,"disableUserChange":false}}
    光标:  C-S {"path":"/s/selections",...,"payload":{"selection":{"sheetId","row","col","deviceId"}}}
    编辑:  C-S {"path":"/s/delta",...,"payload":{"bornTime","guid","contextVersion",
              "protocolVersion":1,"otAlg":"js:1.0","opMeta":{},"type":"normal",
              "operations":"[{\"v\":1,\"action\":\"fillRange\",\"payload\":{...}}]"}}
    确认:  S-C {"path":"/s/delta","payload":{"type":"ack","version":<新版本>},...}
    心跳:  C-S {"path":"/!"} → S-C {"path":"/!","payload":{"version":<当前版本>}}
    存档:  C-S {"path":"/s/checkpoint","payload":{"baseVersion",...}}

单元格操作(表格文档)抓包样例:
    operations=[{"v":1,"action":"fillRange",
                 "payload":{"fillBy":"cell","range":["s1",5,17,1,1],
                            "cell":{"value":"","formula":null}}}]

注意: /s/selections 与 fillRange 的 row/col 均为 0 基且指向同一单元格
(抓包中 selection(5,18)≠range(5,17) 是因为先清空 (5,17) 后光标右移再广播)。

⚠ 版本引导逻辑(contextVersion 初值)是从心跳/ack 观测推断的, 首次真实联调需验证。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from .client import DdocsClient

log = logging.getLogger("ddocs.collab")

COLLAB_WS_URL = "wss://collab-ws.dingtalk.com/ws/editing/v3.2"
OT_ALG = "js:1.0"
PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL = 10.0


def _msg(path: str, headers: dict[str, Any], payload: Optional[dict] = None) -> str:
    body: dict[str, Any] = {"path": path, "headers": headers}
    if payload is not None:
        body["payload"] = payload
    return json.dumps(body, ensure_ascii=False)


def fill_cell_op(sheet_id: str, row: int, col: int, *,
                 value: Optional[str] = None, formula: Optional[str] = None) -> dict:
    """构造一次 fillRange 单元格写操作(抓包同构)。

    range = [sheetId, row, col, rows, cols] — 单元格即 [sheetId, r, c, 1, 1]
    value/formula 至少给一个; 置空传 None。
    """
    return {
        "v": 1,
        "action": "fillRange",
        "payload": {
            "fillBy": "cell",
            "range": [sheet_id, row, col, 1, 1],
            "cell": {"value": value if value is not None else "",
                     "formula": formula},
        },
    }


def update_sheet_op(sheet_id: str, title: str) -> dict:
    """构造 updateSheet 操作(抓包: 重命名工作表)。"""
    return {"v": 1, "action": "updateSheet",
            "payload": {"id": sheet_id, "meta": {"title": title}}}


def dimension_op(sheet_id: str, is_row: bool, frm: int, count: int,
                 insert: bool = True) -> dict:
    """构造 insertDimension / deleteDimension 操作(抓包同构)。"""
    return {
        "v": 1,
        "action": "insertDimension" if insert else "deleteDimension",
        "payload": {"section": {"sheetId": sheet_id, "isRow": is_row,
                                 "from": frm, "count": count}},
    }


def update_dimension_op(sheet_id: str, is_row: bool, frm: int, count: int,
                        size: float) -> dict:
    """构造 updateDimension 操作(行高/列宽; 抓包: size=22, customSize=null)。"""
    populated, empty = ("rows", "cols") if is_row else ("cols", "rows")
    return {
        "v": 1,
        "action": "updateDimension",
        "payload": {
            "section": {"sheetId": sheet_id, "isRow": is_row,
                         "from": frm, "count": count},
            "refs": {populated: [[i, {"size": size, "customSize": None}]
                                  for i in range(count)],
                     empty: []},
        },
    }


class SheetCollabSession:
    """一张表格文档的协同编辑会话(async)。

    Usage
    -----
    >>> async def main():
    ...     async with SheetCollabSession(client, doc_key="Q35O851yQv3dVl9V") as s:
    ...         await s.fill_cell("s1", row=5, col=17, value="hello")
    ...         print(s.version)
    >>> asyncio.run(main())

    doc_key 即 dentry/docKey(= 抓包中的 multiChannelKey)。
    """

    def __init__(self, client: DdocsClient, doc_key: str, *,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL,
                 timeout: float = 10.0,
                 ws_url: str = COLLAB_WS_URL):
        self.client = client
        self.doc_key = doc_key
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout
        self.ws_url = ws_url
        self.awp_connect_id = str(uuid.uuid4())
        self.logic_channel_id = str(uuid.uuid4())
        self.device_id = f"ft:deviceid-{uuid.uuid4().hex[:8]}"
        self.version: Optional[int] = None      # 当前文档版本(从 ack/心跳观测)
        self.current_sheet: Optional[str] = None    # 当前工作表(切换后更新)
        self.known_sheet_ids: set[str] = set()      # 会话内观测到的 sheetId 集合
        self._ws: Any = None
        self._hb_task: Optional[asyncio.Task] = None
        self._mid = 0

    # ------------------------------------------------------------ 内部

    def _headers(self, **extra: Any) -> dict[str, Any]:
        self._mid += 1
        h = {
            "mid": f"{self._mid:x}{int(time.time() * 1000)}",
            "awpConnectId": self.awp_connect_id,
            "multiChannelKey": self.doc_key,
            "logicChannelId": self.logic_channel_id,
        }
        h.update(extra)
        return h

    async def _recv_until(self, predicate, what: str) -> dict:
        """收帧直到 predicate 命中或超时; 超时报告期间观测到的帧路由。"""
        from collections import deque

        assert self._ws is not None
        seen: deque = deque(maxlen=8)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"等待 {what} 超时; 期间收到帧: {list(seen) or '无(连接可能已静默)'}"
                )
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                continue  # 让外层 deadline 判定收尾
            frame = json.loads(raw)
            seen.append(f"{frame.get('path') or frame.get('lwp') or '?'}"
                        f" code={frame.get('code')} status={frame.get('status')}")
            self._observe(frame)
            if predicate(frame):
                return frame

    def _observe(self, frame: dict) -> None:
        """从服务端帧中跟踪文档版本与可见的 sheetId。"""
        payload = frame.get("payload") or {}
        if isinstance(payload, dict):
            if frame.get("path") == "/s/delta" and payload.get("type") == "ack" \
                    and isinstance(payload.get("version"), int):
                self.version = payload["version"]
            elif frame.get("path") == "/!" and isinstance(payload.get("version"), int):
                self.version = payload["version"]
            sel = payload.get("selection")
            if isinstance(sel, dict) and sel.get("sheetId"):
                self.known_sheet_ids.add(str(sel["sheetId"]))
            parts = payload.get("extraPartsOssKey")
            if isinstance(parts, dict):
                self.known_sheet_ids.update(str(k) for k in parts)

    # ------------------------------------------------------------ 工作表

    async def switch_sheet(self, sheet_id: str) -> None:
        """切换当前工作表 = 对目标 sheet 广播 selection(抓包验证的切换形态)。

        编辑任意 sheet 无需先"加载": /s/delta 的 range[0] 就是目标 sheetId。
        """
        await self.send_selection(sheet_id, row=0, col=0)
        self.current_sheet = sheet_id

    def list_sheet_ids(self) -> list[str]:
        """返回会话期间观测到的 sheetId(含他人 selection 暴露的 st-* 子表)。"""
        return sorted(self.known_sheet_ids)

    async def _heartbeat_loop(self) -> None:
        assert self._ws is not None
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                await self._ws.send(_msg("/!", self._headers()))
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover
            log.warning("心跳中断: %r", e)

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> "SheetCollabSession":
        """建连 + /create 建会话 + 启动心跳。"""
        import websockets

        token = self.client.s.get_access_token()
        url = f"{self.ws_url}?token={token}&awpConnectId={self.awp_connect_id}"
        cookie = "; ".join(
            f"{c.name}={c.value}" for c in self.client.s._http.cookies.jar
        )
        headers = {"Cookie": cookie, "Origin": "https://docs.dingtalk.com"}
        log.info("连接协同通道: docKey=%s", self.doc_key)
        try:  # websockets>=13 新实现用 additional_headers, 旧实现用 extra_headers
            self._ws = await websockets.connect(url, additional_headers=headers,
                                                max_size=32 << 20)
        except TypeError:
            self._ws = await websockets.connect(url, extra_headers=headers,
                                                max_size=32 << 20)
        # /create 建会话(抓包: header 里带 docKey)
        await self._ws.send(_msg("/create", self._headers(docKey=self.doc_key),
                                 {"sessionFeature": 0, "disableUserChange": False}))
        await self._recv_until(lambda f: f.get("path") == "/create", "/create 确认")
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def close(self) -> None:
        if self._hb_task:
            self._hb_task.cancel()
            self._hb_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> "SheetCollabSession":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------ 操作

    async def send_selection(self, sheet_id: str, row: int, col: int) -> None:
        """广播本端光标位置(抓包 /s/selections)。"""
        assert self._ws is not None
        self.known_sheet_ids.add(str(sheet_id))
        await self._ws.send(_msg("/s/selections", self._headers(), {
            "selection": {"sheetId": sheet_id, "row": row, "col": col,
                          "deviceId": self.device_id},
        }))

    async def drain(self, seconds: float = 2.0) -> int:
        """被动收帧 seconds 秒, 逐帧观测(版本/sheetId); 返回收到帧数。

        会话常驻时建议周期性调用(或自行起后台任务), 以跟踪他人编辑与切换。
        """
        assert self._ws is not None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        n = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return n
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return n
            self._observe(json.loads(raw))
            n += 1

    async def ping_version(self) -> Optional[int]:
        """发 /! 心跳等待版本回报; 判定放宽为"任何带整数 version 的帧", 并周期性重发。"""
        assert self._ws is not None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        while self.version is None and loop.time() < deadline:
            await self._ws.send(_msg("/!", self._headers()))
            try:
                await asyncio.wait_for(self._wait_any_version_frame(), timeout=4)
            except asyncio.TimeoutError:
                continue  # 重发 /!
        return self.version

    async def _wait_any_version_frame(self) -> None:
        await self._recv_until(
            lambda f: isinstance((f.get("payload") or {}) .get("version"), int)
            if isinstance(f.get("payload"), dict) else False,
            "带 version 的帧",
        )

    async def _submit_delta(self, ops: list[dict], bootstrap_on_reject: bool = True) -> int:
        """提交一组 OT 操作 → 返回 ack 后的文档版本(含版本自动引导重试)。"""
        assert self._ws is not None
        attempt = 0
        while True:
            payload = {
                "bornTime": int(time.time() * 1000),
                "guid": str(uuid.uuid4()),
                "contextVersion": self.version if self.version is not None else 0,
                "protocolVersion": PROTOCOL_VERSION,
                "otAlg": OT_ALG,
                "opMeta": {},
                "type": "normal",
                "operations": json.dumps(ops, ensure_ascii=False),  # 抓包: operations 是 JSON 字符串
            }
            await self._ws.send(_msg("/s/delta", self._headers(), payload))
            frame = await self._recv_until(
                lambda f: f.get("path") == "/s/delta",
                "/s/delta 回复",
            )
            rp = frame.get("payload") or {}
            if rp.get("type") == "ack" and frame.get("status") in (None, 200):
                v = rp.get("version")
                return int(v) if v is not None else (self.version or 0)
            # 被拒: 版本类错误 → 引导版本后重试一次; 其余直接抛
            if bootstrap_on_reject and attempt == 0 and rp.get("code"):
                old_timeout = self.timeout
                self.timeout = max(old_timeout, 45.0)
                try:
                    await self.ping_version()
                finally:
                    self.timeout = old_timeout
                if self.version is not None:
                    attempt += 1
                    continue
            raise RuntimeError(
                f"编辑被服务端拒绝: {json.dumps(frame, ensure_ascii=False)[:400]}"
            )

    async def fill_cell(self, sheet_id: str, row: int, col: int, *,
                        value: Optional[str] = None,
                        formula: Optional[str] = None) -> int:
        """写入一个单元格 → 返回服务端确认后的文档版本。

        value: 纯值(formula=None)  formula: 公式(如 "=SUM(A1:A2)")
        row/col 均为 0 基(与抓包 fillRange 一致; A1 → row=0, col=0)。
        """
        return await self._submit_delta([fill_cell_op(sheet_id, row, col,
                                                      value=value, formula=formula)])

    async def clear_range(self, sheet_id: str, row: int, col: int,
                          rows: int = 1, cols: int = 1) -> int:
        """清空一个区域(抓包 fillBy=clear 同构)。"""
        op = {
            "v": 1,
            "action": "fillRange",
            "payload": {
                "fillBy": "clear",
                "range": [sheet_id, row, col, rows, cols],
                "options": {"value": True, "formula": True, "link": True,
                            "cellType": {"select": True}, "reminder": True,
                            "reaction": True},
                "skipRows": [],
            },
        }
        return await self._submit_delta([op])

    async def add_sheet(self, title: str, *, sheet_id: Optional[str] = None,
                        index: Optional[int] = None) -> str:
        """新建工作表并命名 → 返回 sheetId(客户端生成, 抓包同构)。

        抓包形态: addSheet{meta:{id}} + updateSheet{id, meta:{title}},
        两个操作放在同一次 delta 的 operations 里。
        """
        sid = sheet_id or f"st-{uuid.uuid4().hex[:8]}-{int(time.time() * 1000) % 100000:05d}"
        add = {"v": 1, "action": "addSheet",
               "payload": {"meta": {"id": sid}}}
        if index is not None:
            add["payload"]["index"] = index
        await self._submit_delta([add, update_sheet_op(sid, title)])
        self.known_sheet_ids.add(sid)
        self.current_sheet = sid
        return sid

    async def rename_sheet(self, sheet_id: str, title: str) -> int:
        """重命名工作表(抓包 updateSheet 同构)。"""
        return await self._submit_delta([update_sheet_op(sheet_id, title)])

    async def insert_rows(self, sheet_id: str, frm: int, count: int = 1) -> int:
        """在第 frm 行(0 基)前插入 count 行。"""
        return await self._submit_delta([dimension_op(sheet_id, True, frm, count, True)])

    async def delete_rows(self, sheet_id: str, frm: int, count: int = 1) -> int:
        """删除从 frm 行(0 基)起的 count 行。"""
        return await self._submit_delta([dimension_op(sheet_id, True, frm, count, False)])

    async def insert_cols(self, sheet_id: str, frm: int, count: int = 1) -> int:
        """在第 frm 列(0 基)前插入 count 列。"""
        return await self._submit_delta([dimension_op(sheet_id, False, frm, count, True)])

    async def delete_cols(self, sheet_id: str, frm: int, count: int = 1) -> int:
        """删除从 frm 列(0 基)起的 count 列。"""
        return await self._submit_delta([dimension_op(sheet_id, False, frm, count, False)])

    async def set_row_height(self, sheet_id: str, frm: int, height: float,
                             count: int = 1) -> int:
        """设置行高(0 基第 frm 行起 count 行; 抓包默认行高 22)。"""
        return await self._submit_delta(
            [update_dimension_op(sheet_id, True, frm, count, height)])

    async def set_col_width(self, sheet_id: str, frm: int, width: float,
                            count: int = 1) -> int:
        """设置列宽(0 基第 frm 列起 count 列)。"""
        return await self._submit_delta(
            [update_dimension_op(sheet_id, False, frm, count, width)])

    async def select(self, sheet_id: str, row: int = 0, col: int = 0) -> None:
        """广播光标到指定单元格(切换工作表)。

        注意: 线协议只广播单个光标锚点 —— 矩形选区高亮是各端本地行为,
        不会上报; 矩形操作请直接用 fill_rect/clear_range 等 range 类操作。
        """
        await self.send_selection(sheet_id, row=row, col=col)
        self.current_sheet = sheet_id

    async def set_style(self, sheet_id: str, row: int, col: int,
                        rows: int, cols: int, style: dict) -> int:
        """对一个矩形区域应用内联样式(抓包 fillBy=style 同构)。

        style 是引擎槽位字典, 槽位号→含义由前端引擎定义, 已知线索:
          {"5": 1}          抓包样本(疑似背景/主题色索引)
          {"8": "#000000"}  样式表中的十六进制颜色槽(候选: 字体色)
          {"10"/"12"/"14": "#000000"}  其余颜色槽(候选: 背景色等)
        用 run_style_probe() 可在真实文档上标定槽位含义。
        """
        op = {"v": 1, "action": "fillRange",
              "payload": {"fillBy": "style",
                          "range": [sheet_id, row, col, rows, cols],
                          "style": style}}
        return await self._submit_delta([op])

    # ------------------------------------------------------------ 合并/边框

    async def merge_cells(self, sheet_id: str, row: int, col: int,
                          rows: int, cols: int) -> str:
        """合并矩形区域 → 返回合并要素 id(拆分时需要)。

        抓包同构: insertFeat{feat:"merge", id(客户端生成), range, sheetId}
        """
        feat_id = f"m-{uuid.uuid4().hex[:8]}-{int(time.time() * 1000) % 100000:05d}"
        op = {"v": 1, "action": "insertFeat",
              "payload": {"id": feat_id, "feat": "merge",
                          "range": [sheet_id, row, col, rows, cols],
                          "sheetId": sheet_id}}
        await self._submit_delta([op])
        return feat_id

    async def unmerge_cells(self, sheet_id: str, feat_id: str) -> int:
        """拆分合并(需要 merge_cells 返回的要素 id)。"""
        op = {"v": 1, "action": "deleteFeat",
              "payload": {"id": feat_id, "feat": "merge", "sheetId": sheet_id}}
        return await self._submit_delta([op])

    async def set_border(self, sheet_id: str, row: int, col: int,
                         rows: int, cols: int, *, style: int = 3,
                         color: str = "#000000",
                         site: Optional[dict] = None) -> int:
        """给矩形区域加边框(抓包 fillBy=border 同构)。

        style: 线型编号(抓包样本 3); site 缺省=全边框+内部行列线。
        """
        site = site or {"top": True, "bottom": True, "left": True,
                        "right": True, "innerRow": True, "innerCol": True}
        op = {"v": 1, "action": "fillRange",
              "payload": {"fillBy": "border",
                          "range": [sheet_id, row, col, rows, cols],
                          "border": {"style": style, "color": color},
                          "site": site}}
        return await self._submit_delta([op])

    # 样式槽位号 → 含义(第二轮抓包标定, 用户 UI 实操验证):
    #   2=字体名 {"2": "Microsoft YaHei"}   3=背景填充色 {"3": "#C10002"}
    #   8=字体颜色 {"8": "#000000"}          5/6=开关(加粗/斜体, 0/1/2)
    #   18/19/20=布尔开关(下划线/删除线/换行等)

    async def set_fill_color(self, sheet_id: str, row: int, col: int,
                             rows: int, cols: int, color: str) -> int:
        """设置矩形区域的单元格背景色(如 "#C10002" / "#FCC102")。"""
        return await self.set_style(sheet_id, row, col, rows, cols, {"3": color})

    async def set_font_color(self, sheet_id: str, row: int, col: int,
                             rows: int, cols: int, color: str) -> int:
        """设置矩形区域的字体颜色。"""
        return await self.set_style(sheet_id, row, col, rows, cols, {"8": color})

    async def set_font_name(self, sheet_id: str, row: int, col: int,
                            rows: int, cols: int, name: str) -> int:
        """设置矩形区域的字体(如 "Microsoft YaHei")。"""
        return await self.set_style(sheet_id, row, col, rows, cols, {"2": name})

    async def fill_rect(self, sheet_id: str, row: int, col: int,
                        values: list[list]) -> int:
        """矩形批量写入: values 为二维矩阵(None 跳过), 全部合并进一次 delta。

        字符串以 "=" 开头视为公式。返回 ack 后的文档版本。
        """
        ops = []
        for i, rowvals in enumerate(values):
            for j, v in enumerate(rowvals):
                if v is None:
                    continue
                formula = v if isinstance(v, str) and v.startswith("=") else None
                value = None if formula else v
                ops.append(fill_cell_op(sheet_id, row + i, col + j,
                                        value=value, formula=formula))
        if not ops:
            raise ValueError("values 里没有任何可写内容")
        return await self._submit_delta(ops)

    async def checkpoint(self, skeleton_oss_key: str = "",
                         checkpoint_key: str = "") -> None:
        """请求服务端存档(抓包 /s/checkpoint; OSS 键通常由服务端在初始加载时下发)。"""
        assert self._ws is not None
        payload: dict[str, Any] = {"baseVersion": self.version or 0}
        if skeleton_oss_key:
            payload["skeletonOssKey"] = skeleton_oss_key
        if checkpoint_key:
            payload["checkpointKey"] = checkpoint_key
        await self._ws.send(_msg("/s/checkpoint", self._headers(), payload))
        await self._recv_until(lambda f: f.get("path") == "/s/checkpoint",
                               "/s/checkpoint 确认")


def open_sheet(client: DdocsClient, dentry_uuid: str) -> SheetCollabSession:
    """便捷入口: 由 dentryUuid 取 docKey 构建会话(仍需 await .connect())。"""
    d = client.get_dentry(dentry_uuid)
    doc_key = d.doc_key or d.dentry_key
    if not doc_key:
        raise ValueError(f"dentry 缺少 docKey: {dentry_uuid}")
    return SheetCollabSession(client, doc_key=doc_key)
