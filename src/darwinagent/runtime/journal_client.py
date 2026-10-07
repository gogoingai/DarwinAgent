"""Persist calls made by existing evaluator clients without changing their protocols."""

from types import SimpleNamespace

from .steps import AwaitingBudget, RequestAbandoned, UnknownRequest


class JournalClient:
    def __init__(self, client, journal):
        self.client, self.journal = client, journal
        self.pending_error = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def chat(self, **kwargs):
        try:
            boundary = getattr(self.client, "_control_boundary", None)
            if boundary is not None:
                boundary("model:" + kwargs.get("role", "score"))
            path, saved = self.journal.reserve("model", kwargs)
        except Exception as exc:
            if getattr(exc, "continuation_signal", False) or isinstance(
                exc, (UnknownRequest, AwaitingBudget, RequestAbandoned)
            ):
                self.pending_error = exc
            raise
        if saved is not None:
            return SimpleNamespace(**saved)
        self.journal.submitted(path)
        request = dict(kwargs, durable=True)
        if self.journal.bypass_cache:
            request["use_cache"] = False
        try:
            result = await self.client.chat(**request)
        except Exception as exc:
            from openai import APIStatusError

            from darwinagent.llm.client import BudgetExceeded

            if isinstance(exc, BudgetExceeded) and not exc.dispatched:
                self.journal.awaiting_budget(path, exc)
                self.pending_error = AwaitingBudget(str(exc))
            elif isinstance(exc, APIStatusError):
                self.journal.fail(path, exc)
                raise
            else:
                self.pending_error = UnknownRequest(
                    f"No response receipt at {path}: {type(exc).__name__}"
                )
            raise self.pending_error from exc
        response = {
            "content": result.content,
            "usage": getattr(result, "usage", {}),
            "model": getattr(result, "model", None),
            "role": kwargs.get("role"),
            "requested_model": getattr(result, "requested_model", getattr(result, "model", None)),
            "provider_model": getattr(result, "provider_model", None),
            "response_id": getattr(result, "response_id", None),
            "cache_hit": getattr(result, "cache_hit", False),
            "elapsed_s": getattr(result, "elapsed_s", 0),
        }
        self.journal.respond(path, response)
        return result
