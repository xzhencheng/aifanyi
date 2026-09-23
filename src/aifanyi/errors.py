class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


def require(condition, code="INVALID_TRANSITION", message="当前状态不允许此操作。", status=409):
    if not condition:
        raise DomainError(code, message, status)
