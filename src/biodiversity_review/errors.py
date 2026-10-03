"""领域异常类型。"""


class DomainError(Exception):
    """领域规则错误的基类。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class StateTransitionError(DomainError):
    """非法的状态迁移。"""


class ValidationError(DomainError):
    """输入数据不满足领域约束。"""
