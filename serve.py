"""Local viewer server (stdlib only).

  .venv\\Scripts\\python.exe serve.py      then open http://localhost:8765
"""

import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent
GAMES = ROOT / "games"
VIEWER = ROOT / "viewer"
GAME_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(VIEWER), **kw)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts[:1] != ["api"]:
            return super().do_GET()
        q = parse_qs(url.query)
        if parts == ["api", "games"]:
            out = []
            if GAMES.exists():
                for d in sorted(GAMES.iterdir(), reverse=True):
                    m = d / "meta.json"
                    if m.exists():
                        meta = json.loads(m.read_text(encoding="utf-8"))
                        meta.pop("board", None)
                        out.append(meta)
            return self._json(out)
        if len(parts) >= 3 and parts[1] == "games" and GAME_ID.match(parts[2]):
            d = GAMES / parts[2]
            if not d.exists():
                return self._json({"error": "no such game"}, 404)
            what = parts[3] if len(parts) > 3 else "meta"
            if what == "meta":
                return self._json(json.loads((d / "meta.json").read_text(encoding="utf-8")))
            if what == "status":
                f = d / "status.json"
                return self._json(json.loads(f.read_text(encoding="utf-8")) if f.exists() else {})
            if what == "events":
                since = int(q.get("since", ["0"])[0])
                f = d / "events.jsonl"
                evs = []
                if f.exists():
                    for line in f.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            break  # partially written last line
                        if ev["seq"] > since:
                            evs.append(ev)
                return self._json(evs)
            if what == "call" and len(parts) > 4:
                want = int(parts[4])
                f = d / "calls.jsonl"
                if f.exists():
                    for line in f.read_text(encoding="utf-8").splitlines():
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("call_id") == want:
                            return self._json(rec)
                return self._json({"error": "no such call"}, 404)
        return self._json({"error": "bad path"}, 404)


def serve(port=8765):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    print("Viewer: http://localhost:8765")
    serve()
