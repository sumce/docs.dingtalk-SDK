"""ddocs — 钉钉文档(docs.dingtalk.com)网页版 API 封装。"""
from .client import DdocsClient
from .errors import ApiError, DdocsError, NotLoggedInError
from .models import Dentry, StarItem, UserInfo

__version__ = "0.1.0"
__all__ = [
    "DdocsClient",
    "DdocsError",
    "ApiError",
    "NotLoggedInError",
    "Dentry",
    "StarItem",
    "UserInfo",
]
