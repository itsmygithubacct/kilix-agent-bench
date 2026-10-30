"""One MCP command tool; trusted VM execution remains outside the client process."""
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading


EXECUTE_TOOL = {
    "name": "execute",
    "description": "Run a literal argv command in the owned disposable task VM. Optional stdin is literal UTF-8 text; cwd is an absolute guest path. Commands are bounded and checked against the required route. stdout/stderr are bounded; truncation is reported. This tool cannot run commands on the controller host.",
    "inputSchema": {"type": "object", "properties": {
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 64},
        "stdin": {"type": "string"}, "cwd": {"type": "string"}},
        "required": ["argv"], "additionalProperties": False},
}
MAX_FRAME = 262144


class Bridge:
    def __init__(self, path, execute):
        self.path, self.execute = Path(path), execute
        self.lock = threading.Lock()
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
                line = self.rfile.readline(MAX_FRAME + 1)
                if len(line) > MAX_FRAME:
                    return
                request = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Expected a request object")
                    response = bridge.dispatch(request)
                except Exception as exc:
                    response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                                "error": {"code": -32603, "message": type(exc).__name__}}
                self.wfile.write(json.dumps(response).encode() + b"\n")

        # The runtime root may exceed AF_UNIX's pathname limit. The directory
        # descriptor names the same socket inode through a short trusted path.
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            address = f"/proc/self/fd/{directory_fd}/{self.path.name}"
            self.server = socketserver.ThreadingUnixStreamServer(address, Handler)
        finally:
            os.close(directory_fd)
        self.server.daemon_threads = True
        os.chmod(path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def dispatch(self, request):
        ident, method = request.get("id"), request.get("method")
        response = {"jsonrpc": "2.0", "id": ident}
        if method == "initialize":
            requested = request.get("params", {}).get("protocolVersion")
            version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2024-11-05"
            result = {"protocolVersion": version, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "kilix-agent-bench", "version": "0.2.2"}}
        elif method in {"ping", "notifications/initialized", "notifications/cancelled"}:
            result = {}
        elif method == "tools/list":
            result = {"tools": [EXECUTE_TOOL]}
        elif method == "tools/call" and request.get("params", {}).get("name") == "execute":
            with self.lock:
                output = self.execute(request["params"].get("arguments"))
            result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}],
                      "isError": output.get("exit_code", 0) != 0}
        else:
            return {**response, "error": {"code": -32601, "message": "Method or tool not available"}}
        return {**response, "result": result}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.path.unlink(missing_ok=True)


def proxy(path):
    """Unprivileged stdio process inside the controller's filesystem namespace."""
    for line in sys.stdin.buffer:
        if len(line) > MAX_FRAME:
            raise RuntimeError("MCP frame exceeds limit")
        request = json.loads(line)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            sock.sendall(line if line.endswith(b"\n") else line + b"\n")
            with sock.makefile("rb") as stream:
                response = stream.readline(MAX_FRAME + 1)
        if "id" in request:
            sys.stdout.buffer.write(response)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    proxy(sys.argv[1])
