"""Stable primitive failure boundary; host exception objects never cross it."""

from darwinagent.operators.sandbox import SandboxError


class PublicToolError(ValueError):
    """Trusted tools declare fixed public codes; other messages remain private.

    Subclasses must enumerate credential-free codes in public_codes. Arbitrary
    exception text and unrecognized arguments are never forwarded.
    """

    public_codes = frozenset()


class ToolRejectedBeforeEffect(PublicToolError):
    """Trusted host callback rejected an operation before its declared effect.

    Raise only for declared public codes before the target is mutated. This is
    not a retry promise: exceptions after effects, timeouts, and cancellations
    must remain ordinary exceptions and retain unknown write receipts.
    """


class ToolFailure(RuntimeError):
    """A known primitive failure, identical on original dispatch and receipt replay.

    error_type is the host exception class name. message is a safe, public summary;
    arbitrary host exception messages (which may contain credentials) are not persisted.
    """

    agent_signal = True

    def __init__(self, tool_name, error_type, message):
        if not all(type(v) is str and v for v in (tool_name, error_type, message)):
            raise ValueError("Invalid structured tool failure")
        self.tool_name, self.error_type, self.message = tool_name, error_type, message
        super().__init__(message)

    def to_dict(self):
        return {"tool_name": self.tool_name, "error_type": self.error_type, "message": self.message}

    @classmethod
    def from_exception(cls, tool_name, error):
        kind = type(error).__name__
        message = f"{tool_name} failed ({kind})"
        if isinstance(error, PublicToolError) and len(error.args) == 1:
            code = error.args[0]
            if type(code) is str and code in error.public_codes:
                message = code
        return cls(tool_name, kind, message)


class MacroFailure(ToolFailure, SandboxError):
    """Settled restricted-program failure; also catchable by legacy SandboxError callers."""

    def __init__(
        self,
        tool_name,
        error_type,
        message,
        *,
        error_code="restricted_execution_failed",
        completed_receipts=(),
    ):
        super().__init__(tool_name, error_type, message)
        self.error_code = error_code
        self.completed_receipts = tuple(completed_receipts)

    def to_dict(self):
        result = super().to_dict()
        result["completed_receipts"] = list(self.completed_receipts)
        result["error_code"] = self.error_code
        return result
