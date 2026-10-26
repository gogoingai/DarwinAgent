"""Host-owned current-version read evidence gate for native judge success."""

import hashlib
from pathlib import Path

from darwinagent.runtime.artifacts import digest
from darwinagent.runtime.steps import StepJournal


class ReadCoverageCheck:
    def __init__(self, workspace, episodes):
        self.workspace = Path(workspace).resolve()
        self.episodes = Path(episodes).resolve()

    def identity(self):
        return {
            "protocol": "current-deliverable-read-coverage-v1",
            "workspace": str(self.workspace),
            "episodes": str(self.episodes),
            "scope": "all delivered files under result, latest judge turn only",
        }

    def check(self, stage, candidate):
        if stage != "final_delivery" or candidate.get("status") != "completed":
            return True
        from src.lift.eval.run_task import _extract_judge_result

        try:
            verdict = _extract_judge_result(candidate.get("final_response", ""))
        except ValueError:
            return True  # Native protocol handles malformed verdicts; this grants no success.
        if not verdict.success:
            return True
        try:
            return self.complete_reads()
        except (ValueError, KeyError, OSError, UnicodeError, TypeError):
            return False

    def complete_reads(self):
        if self.workspace.is_symlink() or any(p.is_symlink() for p in self.workspace.rglob("*")):
            return False
        files = sorted(p for p in (self.workspace / "result").rglob("*") if p.is_file())
        if not files:
            return False
        turns = [p for p in self.episodes.glob("turn-*") if p.is_dir() and p.name[5:].isdigit()]
        if not turns:
            return False
        latest = max(turns, key=lambda p: int(p.name[5:]))
        outputs = []
        journal = StepJournal(latest / "steps", create_root=False)
        for path in sorted((latest / "steps").glob("*.json")):
            _, row = journal.read_active(path)
            if row.get("kind") != "tool" or row.get("state") != "responded":
                continue
            if row["response_digest"] != digest(row["response"]):
                return False
            if row.get("input", {}).get("name") == "read_text":
                output = row["response"].get("output", {})
                if "content" in output and "path" in output:
                    outputs.append(output)
        for file in files:
            data = file.read_bytes()
            text = data.decode("utf-8")
            checksum = hashlib.sha256(data).hexdigest()
            ranges = []
            for out in outputs:
                if (self.workspace / out["path"]).resolve() != file.resolve() or out.get(
                    "sha256"
                ) != checksum:
                    continue
                content = out["content"]
                if "offset" not in out:
                    if content == text and hashlib.sha256(content.encode()).hexdigest() == checksum:
                        ranges.append((0, len(text)))
                else:
                    start, end = out["offset"], out["end_offset"]
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start <= end <= len(text)
                    ):
                        return False
                    if (
                        out["total_chars"] != len(text)
                        or text[start:end] != content
                        or hashlib.sha256(content.encode()).hexdigest() != out["content_sha256"]
                    ):
                        return False
                    ranges.append((start, end))
            covered = 0
            for start, end in sorted(ranges):
                if start > covered:
                    break
                covered = max(covered, end)
            if not ranges or covered != len(text):
                return False
        return True
