"""统一错误类型。"""

from __future__ import annotations


class DdocsError(Exception):
    """ddocs 所有错误的基类。"""


class NotLoggedInError(DdocsError):
    """未登录或会话失效 —— 提示执行 `ddocs login`。"""


class ApiError(DdocsError):
    """服务端返回的业务错误(响应信封归一后)。

    docs 域信封:  {status, isSuccess, data} / {status:401, code, timestamp}
    login 域信封: {success, result} / {success:false, errorCode, errorMsg}
    """

    def __init__(self, message: str, *, status: int = 0, code: str = "", endpoint: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.endpoint = endpoint

    def __str__(self) -> str:  # pragma: no cover
        loc = f" [{self.endpoint}]" if self.endpoint else ""
        cod = f" code={self.code}" if self.code else ""
        sta = f" status={self.status}" if self.status else ""
        return f"{super().__str__()}{loc}{cod}{sta}"
