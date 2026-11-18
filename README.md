# ddocs — 钉钉文档网页版 SDK（Python）

对 `docs.dingtalk.com` **网页版 API** 的非官方 Python 封装。当前版本覆盖三件事 + 扫码登录：

| 能力 | 命令 / API | 依据 |
|---|---|---|
| 扫码登录 | `ddocs login` | `login.dingtalk.com` OAuth 二维码链路（纯 HTTP，无需浏览器） |
| 列出文件 | `ddocs ls [目录UUID]` / `client.list_folder()` | `GET /box/api/v2/dentry/list` |
| 查看收藏 | `ddocs fav` / `client.favorites()` | `GET /box/api/v2/star/list` |
| 下载文件 | `ddocs download <UUID>` / `client.download()` | `operationGuard` → `file/download` OSS 预签名 |
| 最近访问 | `ddocs recent` / `client.recent()` | `GET /portal/api/v1/recent/list` |
| 在线编辑入口 | `ddocs open <UUID>` / `client.open_editor()` | `POST /box/api/v2/dentry/launch` |

> 协议依据与端点全集见 [docs/API_ANALYSIS.md](docs/API_ANALYSIS.md)（来自真实抓包 320 条流量的逆向分析）。

## 安装

```bash
pip install -e .            # 运行依赖: httpx pydantic typer qrcode
pip install -e ".[rich]"    # 可选: rich 表格输出
```

## 快速开始

```bash
# 1. 扫码登录（终端渲染二维码, 手机钉钉扫一扫; Cookie 存 ~/.ddocs/credentials.json）
ddocs login

# 2. 列出个人空间根目录
ddocs ls
ddocs ls <目录dentryUuid> --cursor <loadMoreId>   # 翻页

# 3. 查看收藏
ddocs fav
ddocs fav --top            # 置顶

# 4. 下载（自动过组织下载风控 → OSS 预签名 → 流式落盘）
ddocs download <文件dentryUuid> -o ./downloads

# 5. 在浏览器中打开在线编辑/预览
ddocs open <文件dentryUuid>
```

Python API：

```python
from ddocs import DdocsClient

c = DdocsClient()                      # 自动加载已保存的登录态
print(c.me().user.nick)
for f in c.iter_folder():              # 目录全量遍历(自动翻页)
    print(f.name, f.dentry_uuid, f.file_size)
for s in c.favorites():                # 收藏
    print(s.name, s.dentry_uuid)
path = c.download("4lgGw3P8vRpmObp0CgxKGxpZ85daZ90D", dest_dir="./dl")
c.open_editor("4lgGw3P8vRpmObp0CgxKGxpZ85daZ90D")   # 浏览器打开编辑
```

## 认证机制（SDK 自动处理）

- Cookie 会话整体携带（含 `tfstk/isg/cna` 等阿里风控 Cookie，**不伪造、只透传**）
- `XSRF-TOKEN` Cookie → `x-xsrf-token` 请求头自动回显
- `a-token` 头按需向 `/portal/api/v1/token/getAccessToken` 获取，401 自动刷新一次
- 凭证文件 `~/.ddocs/credentials.json` 权限 0600；`ddocs logout` 清除

## 集成到你的代码

### 方式一：从 Git 仓库装（推荐，随仓库更新）

```bash
pip install git+https://your-git/ddocs.git          # 公网/内网 Git 均可
pip install "ddocs[rich] @ git+https://your-git/ddocs.git"   # 带可选依赖
```

### 方式二：本地目录 / 已有代码副本

```bash
pip install D:\Workspace\docs.dingtalk-SDK           # 本地路径
pip install -e D:\Workspace\docs.dingtalk-SDK        # 可编辑模式(改源码即生效)
```

### 方式三：构建 wheel 分发（拷给同事 / 私有 PyPI）

```bash
pip install build && python -m build                 # 产出 dist/ddocs-0.1.0-py3-none-any.whl
pip install dist/ddocs-0.1.0-py3-none-any.whl        # 对方直接装
# 或传私有 PyPI: twine upload --repository-url <内网源> dist/*
```

### 库调用（带完整类型标注，PEP 561 `py.typed` 已声明）

```python
from ddocs import DdocsClient, Dentry

client = DdocsClient()               # 自动加载 ~/.ddocs/credentials.json
client.login()                       # 无凭证时先扫码

# 类型标注可直接被 mypy / IDE 检查
files: list[Dentry] = list(client.iter_folder(page_size=100))
xlsx = next(f for f in files if f.extension == "xlsx")
client.download(xlsx.dentry_uuid, dest_dir="./out")

# 协同编辑/工作表(可选依赖 pip install "ddocs[collab,sheets]")
import asyncio
from ddocs.collab import SheetCollabSession, open_sheet

sheets = client.sheets(xlsx.dentry_uuid)       # xlsx 文件: [{id:'s1', name:'汇总', ...}]
meta = client.sheets_protocol(uuid)            # 协议级: delta 流重建的 sheet 元数据
cells = client.read_cells(uuid, "s1")          # 协议级读取单元格内容(明文 delta 重放)

async def edit():
    async with SheetCollabSession(client, doc_key=xlsx.doc_key) as s:
        await s.switch_sheet("s2")                       # 切换工作表
        await s.fill_cell("s2", row=5, col=17, value="hello")
        await s.fill_rect("s1", 3, 0, [["项目", "状态"], ["A", "B"]])  # 矩形批量(单次 delta)
        await s.add_sheet("新表")                        # 新建工作表
        await s.rename_sheet("s1", "汇总表")             # 重命名
        await s.clear_range("s1", 1, 18, 9, 1)           # 清空区域
        await s.insert_rows("s1", 3, 2)                  # 插入行
asyncio.run(edit())
```

> `read_cells`/`sheets_protocol` 是协议级读取（明文 delta 重放，原生 .axls 也可用）；
> 对很老的文档，checkpoint 归档前的内容可能不在 delta 流里（届时用 xlsx 路线）。
> 列出工作表的名字需要 `openpyxl`（`pip install "ddocs[sheets]"`）。

### 标准合规情况

| 项 | 状态 |
|---|---|
| PEP 517/518 构建后端（hatchling） | ✅ |
| PEP 621 项目元数据（pyproject.toml 单源） | ✅ |
| PEP 561 类型标注（`py.typed`） | ✅ |
| console-script 入口（`ddocs` 命令） | ✅ |
| 语义化版本 | ✅（0.1.0，接口未稳前升 0.x） |
| 异步客户端 | 协同编辑层为 async（websockets）；HTTP 层当前同步，v0.2 计划 httpx.AsyncClient 双轨 |

## 关于"在线编辑"

**表格（xlsx/sheet）协议级编辑：已实现**（`ddocs[collab]`）。WS 抓包解析见
[docs/API_ANALYSIS.md §5](docs/API_ANALYSIS.md)：连接 `collab-ws.dingtalk.com/ws/editing/v3.2?token=<a-token>`
→ `/create` → `/s/delta` 提交 OT 操作（`fillRange`）→ 服务端 ack 推进版本 → `/s/checkpoint` 存档。

- 已离线验证报文与抓包逐字段同构（假服务端回放测试）；**首次真实联调待做**（版本引导初值是从心跳/ack 推断的）。
- alidoc 富文本的动作集（insertText 等）本轮抓包未出现，需补抓一次文档类编辑。
- 浏览器兜底：`ddocs open <uuid>` 通过 launch 接口打开官方编辑器。

## 测试

```bash
pip install -e ".[dev]"
pytest
```

测试全部为离线回放（respx mock，响应形态取自真实抓包，凭证均为假值），不访问网络。

## 风险与边界

- 非**公开 API**，接口随前端版本漂移；本 SDK 仅建议用于个人自动化与研究。
- Cookie 即账号凭证，注意保管 `~/.ddocs/credentials.json`。
- 高频请求可能触发阿里风控（限流/验证码）；SDK 内建 0.2s 最小间隔与退避重试，请勿移除。
