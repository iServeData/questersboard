class DomainError(Exception):
    code = "error"
    status = 400

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail


class PermissionDenied(DomainError):
    code, status = "forbidden", 403


class NotFound(DomainError):
    code, status = "not_found", 404


class Invalid(DomainError):
    code, status = "invalid", 422


class Conflict(DomainError):
    code, status = "conflict", 409


class InvalidTransition(DomainError):
    code, status = "invalid_transition", 409
