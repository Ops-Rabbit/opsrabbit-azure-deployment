"""Fixed, credential-free kernel feasibility diagnostic. Not an execution API."""

import json
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            result = {"ok": True}
        elif self.path.split("?")[0] == "/probe":
            command = [
                "bwrap",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--die-with-parent",
                "--new-session",
                "--ro-bind",
                "/usr",
                "/usr",
                "--symlink",
                "usr/bin",
                "/bin",
                "--symlink",
                "usr/lib",
                "/lib",
                "--symlink",
                "usr/lib64",
                "/lib64",
                "--bind",
                "/workspace",
                "/workspace",
                "--tmpfs",
                "/tmp",
                "--chdir",
                "/workspace",
                "--clearenv",
                "--setenv",
                "PATH",
                "/usr/bin:/bin",
                "--",
                "/bin/sh",
                "-c",
                "test ! -e /proc/self/environ && test ! -e /etc/passwd && printf isolation-ok",
            ]
            try:
                proc = subprocess.run(
                    command, capture_output=True, text=True, timeout=20
                )
                result = {
                    "exitCode": proc.returncode,
                    "stdout": proc.stdout[:1024],
                    "stderr": proc.stderr[:1024],
                }
            except subprocess.TimeoutExpired:
                result = {"timedOut": True}
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
