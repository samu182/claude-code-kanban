"""カンバンの地図（map.html）を手元のブラウザに出すための、小さな配信の口。
board.py が書く ~/.herdr/kanban-state.json をそのまま渡すだけ（判断は持たない）。

  python map_server.py   → http://127.0.0.1:8765/

127.0.0.1 だけで待つ（同じネットワークの他の機械からは見えない）。すでに動いていれば何もせずに終わる。
"""

import http.server
import socket
import sys
from pathlib import Path

PORT = 8765
HERE = Path(__file__).resolve().parent
STATE = Path.home() / ".herdr" / "kanban-state.json"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send(HERE / "map.html", "text/html; charset=utf-8")
        elif path == "/state":
            self.send(STATE, "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def send(self, file, ctype):
        try:
            body = file.read_bytes()
        except OSError:
            self.send_error(503, "まだ組み立てられていない")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def running():
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


if __name__ == "__main__":
    if running():
        sys.exit(0)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
