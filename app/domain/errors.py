"""Domain errors. The API layer maps each of these to an HTTP status code."""


class TimeOffError(Exception):
    """Base class for all expected business-rule errors."""


class NotFoundError(TimeOffError):
    pass


class ValidationError(TimeOffError):
    pass


class InsufficientBalanceError(TimeOffError):
    pass


class InvalidTransitionError(TimeOffError):
    pass


class PermissionDeniedError(TimeOffError):
    pass
