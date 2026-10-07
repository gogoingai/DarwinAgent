"""Installed-wheel acceptance; invoke with the installed environment's Python, outside checkout."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def check():
    with tempfile.TemporaryDirectory(prefix="darwinagent-wheel-") as tmp:
        root = Path(tmp)

        def command(*args):
            return subprocess.run(
                [sys.executable, "-m", "darwinagent", *args],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
                timeout=90,
            )

        command("doctor", "--output", str(root / "doctor"))
        first = command("demo", "--mode", "replay", "--rounds", "2", "--output", str(root / "demo"))
        result = json.loads((root / "demo/demo-summary.json").read_text())
        assert [d["accepted"] for d in result["summary"]["rounds"]] == [True, False]
        rounds = result["summary"]["rounds"]
        assert rounds[0]["baseline"]["metrics"]["accuracy"] == 0.5
        assert [d["candidate"]["metrics"]["accuracy"] for d in rounds] == [1.0, 1.0]
        assert rounds[1]["base_version"] == rounds[0]["candidate_version"]
        assert result["summary"]["adopted_version"] == rounds[0]["candidate_version"]
        before = (root / "demo/optimization/wiki.json").read_bytes()
        command(
            "demo", "--mode", "replay", "--rounds", "2", "--output", str(root / "demo"), "--resume"
        )
        assert before == (root / "demo/optimization/wiki.json").read_bytes()
        print(first.stdout)
        print("Installed-wheel acceptance passed outside checkout.")


if __name__ == "__main__":
    check()
