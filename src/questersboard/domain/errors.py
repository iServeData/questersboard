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


class RateLimited(DomainError):
    code, status = "rate_limited", 429


class InvalidTransition(DomainError):
    code, status = "invalid_transition", 409


class NeedsLeave(Invalid):
    """A character edit would no longer fit events it's signed up for. `events` lists them so the
    page can offer "Leave these events and save"."""

    def __init__(self, message: str, events: list[dict]):
        super().__init__(message)
        self.events = events


class EditionLocked(Invalid):
    """The edition can't change after the character has been in events; the page offers "add as new"."""
