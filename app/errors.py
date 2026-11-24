"""统一的请求错误类型。

所有因为「用户提交的数据不合法 / 无法解析」导致的失败都抛出
:class:`TroubleshootError`，由 FastAPI 注册的异常处理器转成 HTTP 4xx。
"""


class TroubleshootError(Exception):
    """排障请求本身的错误（输入不合法、tar 无法读取等）。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TarReadError(TroubleshootError):
    """上传的层不是可读取的未压缩 tar。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=400)


class InvalidInputError(TroubleshootError):
    """语义不合法的输入（默认 422）。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=422)


class NotFoundError(TroubleshootError):
    """按名查找的资源不存在（404）。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=404)


class ConflictError(TroubleshootError):
    """与现有状态冲突（409），如卷名被残留目录占用。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class VolumeStorageError(TroubleshootError):
    """持久卷写盘失败（500）；卷未生效，可重新初始化。"""

    def __init__(self, message: str):
        super().__init__(message, status_code=500)
