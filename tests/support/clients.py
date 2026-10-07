"""Recorded transport with controller ledger support."""

from darwinagent.llm.recorded import RecordedClient


class LedgerRecordedClient(RecordedClient):
    async def aclose(self):
        pass

    def ledger_summary(self):
        return {"total_calls": len(self.calls)}
