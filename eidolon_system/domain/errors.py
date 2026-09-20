"""Stable System Manager errors independent of transport and host platform."""


class SystemManagerError(Exception):
    """Base class for expected System Manager failures."""


class InvalidManifest(SystemManagerError):
    pass


class NotFound(SystemManagerError):
    pass


class NotReady(SystemManagerError):
    pass


class Conflict(SystemManagerError):
    pass


class RevisionConflict(Conflict):
    pass


class IdempotencyConflict(Conflict):
    pass


class PowerOffRejected(SystemManagerError):
    """Power operation was refused before a successful command acceptance."""


class HostOperationFailed(SystemManagerError):
    pass


class StateStoreFailed(SystemManagerError):
    """The durable operation could not be read or committed."""
