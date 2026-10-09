"""Read actual native-compatible Langfuse traces and run pinned native postprocessing."""

import argparse
import json
import os
from pathlib import Path

from dotenv import dotenv_values

from datasets.lift.register import register, upstream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--trace-env", required=True)
    args = parser.parse_args()
    values = dotenv_values(args.trace_env)
    os.environ["LANGFUSE_PUBLIC_KEY"] = values["LANGFUSE_INIT_PROJECT_PUBLIC_KEY"]
    os.environ["LANGFUSE_SECRET_KEY"] = values["LANGFUSE_INIT_PROJECT_SECRET_KEY"]
    os.environ["LANGFUSE_BASE_URL"] = "http://127.0.0.1:13000"
    os.environ["DO_TRAJECTORY_JUDGE"] = "false"
    upstream(args.upstream)
    register(lambda options: None)
    from langfuse import get_client
    from src.postprocess.run_post_process import run_post_process_pipeline
    from src.report.langfuse_trace_parse import parse_plugin_metadata

    from darwinagent.runtime.artifacts import atomic_json

    root = Path(args.root).resolve()
    client = get_client()
    checked = []
    for session_root in sorted((root / "sessions").iterdir()):
        sid = session_root.name
        saved = json.loads((session_root / "trace-state.json").read_text())
        traces = client.api.trace.list(session_id=sid, name="darwinagent-plugin", limit=100).data
        assert traces, sid
        generations, tool_spans, cumulative = 0, 0, 0
        for trace in traces:
            full = client.api.trace.get(trace.id)
            metadata = parse_plugin_metadata(full.metadata)
            assert metadata is not None
            assert metadata.tool_call_blocks == len(full.output["tool_calls"])
            blocks = [
                b
                for m in metadata.messages
                if isinstance(m, dict) and isinstance(m.get("content"), list)
                for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "toolCall"
            ]
            assert len(blocks) == metadata.tool_call_blocks
            cumulative = max(cumulative, metadata.tool_call_blocks)
            generations += sum(o.type == "GENERATION" for o in full.observations)
            tool_spans += sum(
                o.type == "TOOL" or (o.metadata or {}).get("kind") == "tool"
                for o in full.observations
            )
        assert generations == len(saved["requests"]), (sid, generations, saved)
        assert cumulative == tool_spans == saved["leaves"], (sid, cumulative, tool_spans, saved)
        checked.append(
            {
                "session": sid,
                "roots": len(traces),
                "generation_increments": generations,
                "tool_span_increments": tool_spans,
                "root_tool_count_max": cumulative,
            }
        )
    os.chdir(root)
    run_id = "darwin-" + root.name
    report_path = root / "results" / run_id / "report.json"
    run_post_process_pipeline(run_id, report_path, agent_source="darwinagent")
    outputs = [
        str(p.relative_to(root))
        for p in (root / "results").rglob("*")
        if p.suffix in {".csv", ".html"}
    ]
    assert sum(p.endswith(".csv") for p in outputs) >= 2, outputs
    assert any(p.endswith(".html") for p in outputs), outputs
    atomic_json(
        root / "trace-acceptance.json",
        {
            "sessions": checked,
            "native_postprocess": outputs,
            "actual_model_http": 0,
            "trajectory_judge": "not_run",
            "synthetic_recorded_usage": True,
        },
    )
    client.shutdown()
    print(json.dumps({"status": "PASS", "sessions": len(checked), "outputs": outputs}))


if __name__ == "__main__":
    main()
