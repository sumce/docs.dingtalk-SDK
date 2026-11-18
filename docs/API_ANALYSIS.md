# docs.dingtalk.com（网页版钉钉文档）API 逆向分析

> 数据源: `burp.xml`（Burp Suite 2026.10.1 导出，320 条 HTTP 事务）
> 解析工具: [`tools/parse_burp.py`](../tools/parse_burp.py)（iterparse 分片流式解析，内存 O(1)）
> 产物: `analysis/items.jsonl`（逐条归一化）、`analysis/report.json`（聚合统计，已脱敏）

---

## 1. 捕获概览

| 维度 | 分布 |
|---|---|
| 总事务 | 320 |
| Host | `docs.dingtalk.com` 149 · `csp.dingtalk.com` 57（CSP 上报，噪音）· `*.log.aliyuncs.com` 50（SLS 埋点，噪音）· `login.dingtalk.com` 11 · `webalfa-cm3.dingtalk.com` 3（WS）· `127.0.0.1:8440` 4（钉钉客户端本地桥）· `store.dingtalk.com` 3 |
| 方法 | POST 161 / GET 159 |
| 状态 | 200×246、400×49（全是埋点）、101×3（WS 升级）、302×2、500×1、401×1 |

> ⚠️ `csp.dingtalk.com/csp` 是 **Content-Security-Policy 违规上报**（`application/csp-report`），
> 与"协同会话协议"无关，SDK 中应直接忽略。

## 2. 服务划分与端点地图

### 2.1 `portal` — 门户/入口服务（`/portal/api/v1|v2`）

| 方法 | 端点 | 关键参数 | 作用 |
|---|---|---|---|
| GET | `/portal/api/v1/recent/list` | `pageSize, resourceCorpId, operateTypes, creatorType, excludeTypesInOther` | 最近访问列表（分页 `hasMore`） |
| POST | `/portal/api/v1/share/listv2` | body: `pageSize, sortType, shareType, orderBy, accessTypes…` | 我发出/收到的分享列表 |
| GET | `/portal/api/v1/share/member/list/default/all` | `pageSize, sceneCode` | 可分享成员/部门 |
| GET | `/portal/api/v2/unreadChatFile/count`、`POST …/list` | `types, sendTimeRange` | 会话中未读文件 |
| GET | `/portal/api/v1/mine/info` | — | 当前用户（uid/name/脱敏手机号） |
| GET | `/portal/api/v1/mine/orgs` | `orgTypes` | 所属组织列表（corpId/orgId/orgName） |
| GET | `/portal/api/v1/star/get`、`GET /box/api/v2/star/list` | `dentryUuid` | 收藏状态/收藏夹 |
| POST | `/portal/api/v1/token/getAccessToken` | body `{}` | **换取 accessToken**（对应 `a-token` 头） |
| GET | `/portal/api/v1/msgbox/badge/list`、`selfSetting*` | — | 消息角标、个人设置 |

### 2.2 `box` — 网盘/文件盒服务（`/box/api/v1|v2`）★ 核心

| 方法 | 端点 | 关键参数 | 作用 |
|---|---|---|---|
| GET | `/box/api/v2/dentry/list` | `dentryUuid, withParentAncestors, orderType, sortType, pageSize` | 目录浏览（返回 `children` + `positionCursor` 游标分页） |
| GET | `/box/api/v2/dentry/info` | `dentryUuid` | 单文件/夹元数据（全字段见 §4） |
| POST | `/box/api/v2/dentry/launch` | body: `dentryUuid, launchType=PREVIEW, extraParam.scene` | 打开文档 → 返回 `launchInfo`（编辑器 iframe 入口） |
| GET | `/box/api/v2/file/download` | `dentryUuid, downloadType=URL_PRE_SIGNATURE` | 获取 **OSS 预签名下载 URL** |
| POST | `/box/api/v1/dentry/operationGuard` | body: `operationType=DOWNLOAD, resourceIdList, includeChildren` | 操作前置风控/权限检查（`isBlocked`） |
| GET | `/box/api/v2/dentryuuid/exchangeid` | `dentryUuid` | UUID → `dentryId/spaceId/corpId` 内部 ID 互换 |
| GET | `/box/api/v1/teamfile/space/list`、`/box/api/v1/mine/space/info`、`/box/api/v2/mine/*` | — | 团队空间、个人空间、配额 |
| GET | `/box/api/v2/star/list|top/list`、`/box/api/v1/task/mine/recent`、`/box/api/v2/user/setting`、`/box/api/v1/config/getGlobalConfig`、`/box/api/v1/gray/*` | — | 收藏、任务、配置、灰度 |

### 2.3 `nd` — 节点/文档资源服务（`/nd/api/v1|v2`）

| 方法 | 端点 | 关键参数 | 作用 |
|---|---|---|---|
| GET | `/nd/api/v2/resource/share_url` | `resourceId, resourceType=DENTRY` | 生成分享链接（`mobileUrl/pcUrl`） |
| GET | `/nd/api/v2/resource/menus` | `resourceType, resourceId, sceneCode` | 右键菜单能力集 |
| GET | `/nd/api/v1/dentry/path` | `dentryUuid` | 面包屑路径（`pathInfo`） |
| GET | `/nd/api/v1/dentry/ext_info` | — | 扩展信息（本次捕获 500，接口存在） |

### 2.4 其它

| 前缀 | 端点 | 作用 |
|---|---|---|
| `/core/api` | `POST /core/api/comment/list` | 评论列表（`objectId=dentryUuid`, `bizCode=2`, 分页） |
| `/api` | `POST /api/users/getUserInfo` | 前端全局用户态（含 `accessToken`、orgs、偏好） |
| `/api` | `POST /api/doc/switches`、`POST /api/gray/newCheckGrayKeysStrictly` | 功能开关/灰度（SDK 可忽略） |
| `/i/api` | `GET /i/api/login/status` | 会话有效性探活 |
| `/uni-preview`、`/pw/getPreviewSecurityInfo` | 预览服务（安全配置 + 预览入口） |
| `/bizconfig` | `GET …/rule/getV2` | 组织策略配置 |
| `/dd/api/v1/assistant/*` | AI 助手入口配置 |

## 3. 认证与会话体系（SDK 设计的关键）

1. **Cookie 会话**（148/149 请求携带）：
   - `account`（OAuth 票据，URL-encoded）· `dt_s`（会话）· `doc_atoken`（文档侧 token）· `portal_corp_id` · `deviceid`
   - `tfstk / isg / cna / xlly_s` — 阿里系风控 Cookie（`bx-v` 头同源），**不要模拟生成，随浏览器会话一起带走**
2. **CSRF 双提交**：Cookie `XSRF-TOKEN` → 请求头 `x-xsrf-token` 回显（142 次命中）。SDK 必须自动做这个映射。
3. **`a-token` 请求头**（131 次）：来自 `POST /portal/api/v1/token/getAccessToken`，挂在 `POST /core/api/comment/list` 等写路径上。
4. **组织上下文**：`corp-id` / `orgid` 头切换空间归属（52 + 6 次）。
5. **追踪**：`eagleeye-traceid`（可选，便于对齐抓包）。
6. **登录链路**（`login.dingtalk.com`）：`/oauth2/login_with_qr`（二维码）→ `/i/api/login/status` 轮询；`127.0.0.1:8440 /check_state /check_cloud_print` 是**钉钉桌面客户端本地桥**（扫码确认通道），SDK 不应依赖。
7. **响应信封（三种并存，SDK 要统一归一）**：
   - `{data, isSuccess, status}`（主流，142 次）
   - `{errorCode, errorMessage}`（错误，50 次）
   - `{success, result}`（6 次，登录域）

## 4. 核心数据模型（`Dentry`，从 `dentry/info` 与 `dentry/list` 归纳）

```
Dentry {
  dentryUuid: str          # 全局主键（32 位 base62，URL 中使用）
  dentryId / dentryKey / docKey / driveDentryId: str   # 内部多套 ID
  parentDentryUuid / parentDentryId, ancestorList[], depth
  driveSpaceId, corpId, orgId, driveSceneType   # 归属空间/组织 (sceneType: orgBizMeta|group|mine…)
  name, dentryType(file|folder…), contentType(document|alidoc…), extension(adoc|xlsx|able…)
  fileSize, createdTime/updatedTime(ms), creator{uid,name,staffId}, updator
  hasChildren, isInTrash, illegal, positionCursor   # 列表游标
  dentryStatistic{childrenCount, previewCount, downloadCount}
  securityInfo{forbiddenCopy, forbiddenSpread, watermark}, shareScopeInfo
  extraDTO{filePersistType, fileSecurityStatus}, thumbnail
}
Space { spaceUuid/id, driveSpaceId, rootDentryUuid, name, owner, usedQuota/maxQuota/fileQuota, securityInfo }
Share { dentryUuid, shareScope, url{mobileChildAppUrl…}, creator, createdTime }
Comment { objectId, bizCode=2, customCategory=global, 分页 data+hasMore }
```

ID 体系是 SDK 封装的最大坑：**对外一律用 `dentryUuid`**，调 `exchangeid` 换 `dentryId/spaceId` 给内部接口用。

## 5. 实时通道（websocket_history.xml 抓包, 627 帧, 已解析）

解析工具: [`tools/parse_ws.py`](../tools/parse_ws.py) → `analysis/ws_items.jsonl` + `ws_report.json`
（注意：Burp 该导出 `<content>` 含裸 `&` 不合规，工具用行流式解析容错处理。）

### 5.1 通道一：`wss://webalfa-cm3.dingtalk.com/long` — LWP 长连接网关（552 帧）

- 心跳：`C-S {"lwp":"/!","headers":{"mid"}}` → `S-C {"headers":{"mid",...},"code":200}`
- 注册：`C-S {"lwp":"/reg"}`
- RPC：`C-S {"lwp":"/r/Adaptor/<服务>/<方法>"}`，抓到 `DingTalkDocPortalI/uploadDocSnapshot`、
  `CSpace/preview`、`DingTalkDocI/getPreviewDocumentData` 等（预览/快照走这条）

### 5.2 通道二：`wss://collab-ws.dingtalk.com/ws/editing/v3.2?token=<a-token>&awpConnectId=<uuid>` — 协同编辑（75 帧）

生命周期（全部明文 JSON）：

| 方向 | path | payload 要点 |
|---|---|---|
| C→S | `/create` | headers 带 `docKey(=multiChannelKey)`, `awpConnectId`, `logicChannelId`；payload `{sessionFeature:0, disableUserChange:false}` |
| S→C | `/limit/edit` | `{editStatus:"edit"}` 编辑权状态 |
| S→C | `/s/userChange` | 在线成员（uid/nick/orgName/authLevel） |
| C→S | `/s/selections` | 光标 `{selection:{sheetId:"s1",row,col,deviceId}}` |
| C→S | `/s/delta` | **编辑提交**：`{bornTime, guid, contextVersion:<N>, protocolVersion:1, otAlg:"js:1.0", opMeta:{}, type:"normal", operations:"<JSON字符串数组>"}` |
| S→C | `/s/delta` | **ack**：`{type:"ack", version:<N+1>}` |
| C↔S | `/!` | 心跳，S 帧带 `{version:<当前版本>}` |
| C→S | `/s/checkpoint` | 存档 `{baseVersion, checkpointKey, skeletonOssKey, extraPartsOssKey}`（文档快照存 OSS 的键位） |

操作样例（表格单元格写入，抓包原文）：

```json
[{"v":1,"action":"fillRange",
  "payload":{"fillBy":"cell","range":["s1",5,17,1,1],
             "cell":{"value":"","formula":null}}}]
```

**结论：表格(sheet)的协议级编辑可行且已实现**（`ddocs.collab`，见下）；alidoc 富文本的动作集
（insertText 等）未在本轮抓包中出现，需在文档类文件里编辑一次补抓。
版本引导（首个 contextVersion 初值）从心跳/ack 观测推断，真实联调需验证。

### 5.3 对应实现

- `ddocs/collab.py` — `SheetCollabSession`：connect(`/create`)、`fill_cell()`（/s/delta+ack 版本推进）、
  `switch_sheet()`/`send_selection()`、`add_sheet()`/`rename_sheet()`、`clear_range()`、
  `insert_rows/delete_rows/insert_cols/delete_cols`、`checkpoint()`、自动心跳、
  `ping_version()`（版本引导）、`drain()`（被动观测）；测试 `tests/test_collab.py`。

### 5.4 工作表操作词汇表（第二轮抓包 2026-11-17 20:33 补充）

| action | payload 要点 | 语义 |
|---|---|---|
| `fillRange` | `fillBy:"cell"` / `fillBy:"clear"`(+`options`) | 写单元格 / 清空区域 |
| `addSheet` | `{meta:{id}}`(+`index`) | 新建工作表（**id 由客户端生成**，如 `st-xxxx-xxxxx`） |
| `updateSheet` | `{id, meta:{title}}` | 工作表重命名 |
| `insertDimension` / `deleteDimension` | `{section:{sheetId,isRow,from,count}}` | 插入/删除行列 |
| `insertFeat` / `deleteFeat` | `{feat:"merge", range, sheetId}` | 合并/取消合并 |
| `mirrorRange` | `{mirrorBy:"paste", to, from, styleSlice}` | 粘贴(含样式) |

- **切换工作表没有专用帧**：对目标 sheetId 广播 `/s/selections` 即切换；编辑任意 sheet
  无需先加载 —— `/s/delta` 的 `range[0]` 就是目标 sheetId。
- **样式槽位表**（第三轮抓包: 用户 UI 涂色实操标定）：

  | 槽位 | 含义 | 样例 |
  |---|---|---|
  | 2 | 字体名 | `{"2": "Microsoft YaHei"}` |
  | 3 | **背景填充色** | `{"3": "#C10002"}` |
  | 8 | 字体颜色 | `{"8": "#000000"}` |
  | 5 / 6 | 开关(加粗/斜体类, 0/1/2) | `{"5": 1}` |
  | 9~15 | 字号/边框族(数值) | `{"9": 3}` |
  | 16 | 对齐类(数值) | `{"16": 0}` |
  | 18/19/20 | 布尔开关(换行/删除线等) | `{"20": false}` |

  应用方式: `fillRange fillBy:"style"` + 矩形 range + 槽位字典(部分覆盖, 引擎自动合并)。
  SDK 封装: `set_fill_color()` / `set_font_color()` / `set_font_name()` / `set_style()`。
  **样式可读**: delta 重放器跟踪 style 合并, `read_cells()` 返回每格 `style` 字段。
- **sheetId 双形态**：导入的 xlsx 按 `s1..sN`（序号）；会话内新建的 sheet 是客户端生成的随机 id。
- **sheet 名字的获取是端到端加密难点**：文档 skeleton/checkpoint 分片存储于
  `alidocs-body-zjk-cdn.dingtalk.com/e/collab/{docKey}/{uuid}`，内容为 AES-GCM 密文；
  密钥经 `collab.dingtalk.com/api/collab/v2/exchangeCheckpointKey` 交换
  （`{type,key,salt}` → `{secret, wrapAlgo:KC/KC/AES/GCM/NoPadding}`），解密算法在前端 JS 内，
  抓包无法还原。SDK 采用等价方案：下载原始 xlsx + openpyxl 读名字（顺序 ↔ sN）。
- collab HTTP 服务补充：`getDeltasWithVersion`（增量拉取）、`doc/domain`、
  `editStatus/getUsers`、`generateUploadCertification`（上传凭证）。

### 5.5 版本引导语义（真实联调结论）

- collab 版本是**会话级**的：文档在所有端关闭后重新打开，版本从 0 重新计数；
  网页编辑器等活跃会话存在时则是全局连续版本（如 16260+）。
- 服务器对 `/!` 的版本回报延迟不定（0~60s+），`ping_version()` 已放宽判定并周期重发。
- 版本不匹配被拒（`code=52622005 "exist rollback checkpoint after current base version"`）
  时，`fill_cell`/`_submit_delta` 自动引导真实版本并重试一次。

### 5.6 协议级读取单元格（第三轮联调 2026-11-17 20:30 突破）★

`GET collab.dingtalk.com/api/collab/v2/getDeltasWithVersion?version=0&limit=500`
+ 请求头 **`a-doc-key: <docKey>`** → 返回该版本之后的**明文 OT 操作流**
（`{list:[{version, operations:"<JSON字符串>", userId, ...}]}`）。

**从 version 0 全量拉取并按序重放 = 完整单元格状态**：fillRange(cell/clear) 的值与公式、
addSheet/updateSheet 的 sheet 元数据、insert/deleteDimension 的行列位移全部可重建。
实现: `ddocs/replay.py`（重放器）+ `client.read_cells()` / `client.sheets_protocol()` /
`client.get_deltas()`。

边界与坑：
- 原生格式（.axls/.adoc）**不能走 xlsx 预签名下载**（403），xlsx+openpyxl 路线只适用
  于导入的 xlsx 文件；协议级读取（delta 重放）对原生格式同样有效。
- delta 流只覆盖未归档区间：老文档 checkpoint 归档之前的内容不在流里（重放需以
  checkpoint 版本为起点，而 checkpoint 内容是加密的）→ 新建/近期活跃文档读取完整，
  老文档可能缺早期内容。
- `/s/selections` 线协议只广播单格光标锚点 `{sheetId,row,col,deviceId}`（+isTextEditing），
  矩形选区高亮是各端本地行为，不上报；矩形批量写入已验证
  （多个 fillRange op 合并进一次 delta，实测 2×2/3×2 均单次 ack）。

## 6. 合规与稳定性提示

- 这是**非公开 Web API**：接口随前端版本漂移（灰度开关极多），无 SLA；凭证是个人账号 Cookie，存在泄露与失效风险。
- `bx-v/tfstk/isg` 风控体系表明阿里有设备指纹风控，**高频脚本化访问可能触发限流/风控**。SDK 应做限速、失败退避，且仅用于个人自动化/研究。
- 若目标是正式集成，官方 OpenAPI（open.dingtalk.com）已提供文档/网盘部分接口，Web 协议封装适合作为官方能力之外的补充。
