"""Exercise real HTTP reservation across independent processes against a local SSE server."""

import argparse
import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


async def child(root, port, campaign):
    from darwinagent.config import Config
    from darwinagent.llm.client import BudgetExceeded, LLMClient

    cfg = Config(
        api_base_url=f"http://127.0.0.1:{port}/v1",
        api_key="local-dummy",
        model_strong="offline",
        max_retries=1,
        model_profiles=False,
        request_budget_path=root / "http-attempts.json",
        max_http_requests=1,
        work_dir=root / campaign / "client",
    )
    cfg.role_tiers.update(worker="strong", judge="strong")
    from datasets.lift.transport import SharedTransport

    async with LLMClient(cfg) as client:
        shared = SharedTransport(client, root / "shared")
        shared.run_id = campaign
        scoped = shared.scoped(campaign + "-worker", "worker")
        try:
            result = await scoped.chat(
                role="worker",
                messages=[{"role": "user", "content": "local probe"}],
                durable=True,
                use_cache=False,
            )
            return {
                "status": "completed",
                "content": result.content,
                "attempts": client.http_attempts(),
            }
        except BudgetExceeded as exc:
            return {
                "status": "budget_exhausted",
                "dispatched": exc.dispatched,
                "attempts": client.http_attempts(),
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--campaign", default="campaign-a")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    if args.port:
        print(json.dumps(asyncio.run(child(root, args.port, args.campaign))))
        return
    root.mkdir(parents=True, exist_ok=False)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {
                "id": "local-probe",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "offline",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = [
            sys.executable,
            "-m",
            "datasets.lift.tests.http_budget_probe",
            "--output",
            str(root),
            "--port",
            str(server.server_port),
        ]
        first = json.loads(subprocess.check_output(command, text=True))
        second = json.loads(
            subprocess.check_output([*command, "--campaign", "campaign-b"], text=True)
        )
        third = json.loads(
            subprocess.check_output([*command, "--campaign", "campaign-c"], text=True)
        )
        assert first == {"status": "completed", "content": "ok", "attempts": 1}
        assert second == third == {"status": "budget_exhausted", "dispatched": False, "attempts": 1}
        assert len(requests) == 1
        report = {
            "independent_processes": 3,
            "independent_campaign_roots": 3,
            "shared_http_ledger_and_pause_gate": True,
            "first": first,
            "second": second,
            "third": third,
            "actual_local_http_requests": len(requests),
            "real_model_requests": 0,
        }
        (root / "acceptance.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
