"""Stable domain/application error vocabulary."""


class KernelError(Exception):
    """Base error exposed to delivery adapters."""


class InvalidRequest(KernelError, ValueError):
    """A command violates a local invariant."""


class NotFound(KernelError, KeyError):
    """An authoritative fact does not exist in the requested view."""


class Conflict(KernelError):
    """A request conflicts with current authoritative state."""


class RevisionConflict(Conflict):
    """CAS precondition did not match the current revision."""


class IdempotencyConflict(Conflict):
    """A request_id was reused with a different fingerprint."""


class AuthorityRejected(KernelError):
    """An external authority rejected a prerequisite fact."""


class AuthorityUnavailable(KernelError):
    """A required external authority contract is unavailable."""


class AuthorizationDenied(KernelError, PermissionError):
    """The configured authorization boundary denied access."""
