"""数据模型 — 钉钉文档字段随前端版本漂移, 因此全部 extra="allow"。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="allow",            # 未建模字段保留在 model_extra, 不丢信息
        populate_by_name=True,    # 支持蛇形赋值
        alias_generator=to_camel, # 驼峰 JSON ↔ 蛇形属性
    )


class Org(_Base):
    corp_id: str = ""
    org_id: Optional[int] = None
    name: str = ""
    is_main_org: bool = False


class User(_Base):
    uid: Optional[int] = None
    nick: str = ""


class UserInfo(_Base):
    """POST /api/users/getUserInfo 的归一视图。"""

    user: Optional[User] = None
    orgs: list[Org] = []
    access_token: str = ""
    is_visitor: bool = False


class DentryStatistic(_Base):
    children_count: Optional[int] = None
    preview_count: Optional[int] = None
    download_count: Optional[int] = None


class Creator(_Base):
    uid: Optional[str] = None
    name: str = ""
    staff_id: Optional[str] = None


class Dentry(_Base):
    """文件/文件夹条目 — 对外主键是 dentry_uuid。"""

    dentry_uuid: str = ""
    dentry_id: str = ""
    dentry_key: str = ""
    doc_key: str = ""
    parent_dentry_uuid: str = ""
    name: str = ""
    dentry_type: str = ""          # file | folder ...
    content_type: str = ""         # document | alidoc | file ...
    extension: str = ""
    file_size: Optional[int] = None
    has_children: bool = False
    is_in_trash: bool = False
    created_time: Optional[int] = None   # ms epoch
    updated_time: Optional[int] = None
    creator: Optional[Creator] = None
    drive_space_id: str = ""
    corp_id: str = ""
    org_id: str = ""
    statistic: Optional[DentryStatistic] = Field(default=None, alias="dentryStatistic")
    raw: dict[str, Any] = {}

    def model_post_init(self, __ctx: Any) -> None:
        self.raw = self.model_extra or {}


class DentryPage(_Base):
    """GET /box/api/v2/dentry/list 的归一视图。

    root 在不同版本里可能是对象或布尔(实测漂移), 不做强类型。
    """

    children: list[Dentry] = []
    has_more: bool = False
    load_more_id: str = ""          # 下一页游标
    root: Optional[Any] = None


class RecentItem(_Base):
    """GET /portal/api/v1/recent/list 的条目。"""

    access_time: Optional[int] = None
    operate_type: str = ""
    location: str = ""
    resource: Optional[Dentry] = None
    raw: dict[str, Any] = {}

    def model_post_init(self, __ctx: Any) -> None:
        self.raw = self.model_extra or {}

    @property
    def dentry_uuid(self) -> str:
        return self.resource.dentry_uuid if self.resource else self.raw.get("dentryUuid", "")


class StarItem(_Base):
    """GET /box/api/v2/star/list 条目。

    实测结构: 顶层 {id(=dentryUuid), createTime, location, resource:{Dentry 全字段}},
    顶层同名字段(旧抓包形态)优先, 缺失时回退 resource 内字段。
    """

    dentry_uuid: str = ""
    name: str = ""
    dentry_type: str = ""
    content_type: str = ""
    extension: str = ""
    starred_time: Optional[int] = None
    location: str = ""
    resource: Optional[Dentry] = None
    raw: dict[str, Any] = {}

    def model_post_init(self, __ctx: Any) -> None:
        self.raw = self.model_extra or {}
        # resource 是声明字段(已解析为 Dentry); 顶层旧形态字段优先, 缺失回退 resource
        r = self.resource
        if not self.dentry_uuid:
            self.dentry_uuid = self.raw.get("id") or (r.dentry_uuid if r else "")
        if not self.name:
            self.name = r.name if r else ""
        if not self.dentry_type:
            self.dentry_type = r.dentry_type if r else ""
        if not self.content_type:
            self.content_type = r.content_type if r else ""
        if not self.extension:
            self.extension = r.extension if r else ""
        if self.starred_time is None:
            self.starred_time = self.raw.get("createTime")
        if not self.location:
            self.location = self.raw.get("location", "")

    @property
    def file_size(self) -> Optional[int]:
        return self.resource.file_size if self.resource else None

    @property
    def updated_time(self) -> Optional[int]:
        return self.resource.updated_time if self.resource else None


class SpaceInfo(_Base):
    """GET /box/api/v1/mine/space/info — 个人空间, rootDentryUuid 是 ls 的默认根。"""

    space_uuid: str = ""
    drive_space_id: str = ""
    root_dentry_uuid: str = ""
    name: str = ""
    used_quota: Optional[int] = None
    max_quota: Optional[int] = None


class DownloadInfo(_Base):
    """GET /box/api/v2/file/download → data.ossUrlPreSignatureInfo。"""

    download_type: str = ""
    part_size: int = 1
    pre_sign_urls: list[str] = []
    file_name: str = ""             # 从预签名 URL 的 content-disposition 解析
