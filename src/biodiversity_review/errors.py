"""领域错误：API 层据此映射为合适的 HTTP 状态码。"""


class ReviewError(Exception):
    """所有审查业务错误的基类。"""

    http_status = 400


class NotFound(ReviewError):
    http_status = 404


class ConflictError(ReviewError):
    """请求与当前状态冲突（含重复申报检测）。"""

    http_status = 409


class ValidationError(ReviewError):
    http_status = 422


class InvalidTransition(ReviewError):
    http_status = 409
