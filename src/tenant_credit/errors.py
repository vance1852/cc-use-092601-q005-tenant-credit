"""多租户算力信用额度服务向 API 和 CLI 暴露的稳定错误。"""


class CreditError(RuntimeError):
    code = "credit_error"
    status = 400


class NotFound(CreditError):
    code = "not_found"
    status = 404


class Conflict(CreditError):
    code = "conflict"
    status = 409


class InsufficientCredit(Conflict):
    code = "insufficient_credit"
    status = 409


class Forbidden(CreditError):
    code = "forbidden"
    status = 403


class InvalidState(CreditError):
    code = "invalid_state"
    status = 409


class ValidationFailed(CreditError):
    code = "validation_failed"
    status = 422
