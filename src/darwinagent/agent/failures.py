"""Stable primitive failure boundary; host exception objects never cross it."""


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
        return cls(tool_name, kind, f"{tool_name} failed ({kind})")
