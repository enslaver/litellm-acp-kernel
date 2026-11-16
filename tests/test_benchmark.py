import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("benchmark", Path(__file__).parent.parent / "scripts" / "benchmark.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)

CONVERSATION = [
    {"role": "user", "content": "one"},
    {"role": "assistant", "content": "a"},
    {"role": "user", "content": "two"},
    {"role": "assistant", "content": "b"},
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # the acp arm reports half the prompt tokens
        prompt = len(body["messages"]) * 100 // (2 if body["model"] == "acp" else 1)
        out = json.dumps({"usage": {"prompt_tokens": prompt, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": prompt // 2}}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def test_benchmark_compares_arms(tmp_path, capsys):
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = tmp_path / "c.json"
    path.write_text(json.dumps(CONVERSATION))
    try:
        code = benchmark.main([str(path), "--base-url", f"http://127.0.0.1:{server.server_port}", "--baseline", "base", "--acp", "acp", "--price-in", "1", "--price-out", "2"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 0
    assert "baseline   requests=2 prompt=400" in out
    assert "acp        requests=2 prompt=200" in out
    assert "prompt tokens vs baseline: -50.0%" in out
    assert "cache_read_share=50.0%" in out


def test_benchmark_rejects_conversation_without_assistant_turns(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"role": "user", "content": "hi"}]))
    assert benchmark.main([str(path), "--baseline", "a", "--acp", "b"]) == 2
