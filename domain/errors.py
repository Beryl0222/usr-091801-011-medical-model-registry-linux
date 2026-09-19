"""领域错误类型，HTTP 层据此映射状态码。"""


class DomainError(Exception):
    """所有领域错误的基类，http_status 给出建议状态码。"""

    http_status = 400


class ValidationError(DomainError):
    """输入不满足准入规则（含患者级字段、协议不符、指标不可信等）。"""

    http_status = 422


class AuthError(DomainError):
    """缺少或持有无效的机构令牌。"""

    http_status = 401


class ForbiddenError(DomainError):
    """越权访问其他机构数据或无权执行该操作。"""

    http_status = 403


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    """重复登记、重复审批或指纹冲突。"""

    http_status = 409


class StateError(DomainError):
    """生命周期状态不允许该迁移，或审批/证据前置条件不满足。"""

    http_status = 409
