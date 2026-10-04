"""Tiny static server for the editor/player with a settings save endpoint."""
from __future__ import annotations

import io
import mimetypes
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .package_validate import ValidationError, json_bytes, require
from .playback_settings import resolve_playback_settings
from .production_io import atomic_json

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/json", ".json")


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        # Optional editor tuning is represented explicitly as an empty object.
        # This avoids speculative404 requests and retains normal saved settings.
        target = Path(self.translate_path(self.path))
        root = Path(self.directory).resolve()
        if (target.name == "settings.json" and not target.exists()
                and root in target.resolve().parents
                and (target.parent / "model.json").is_file()):
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return io.BytesIO(payload)
        return super().send_head()

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/settings":
            self.send_error(404)
            return
        model = parse_qs(url.query).get("model", [""])[0]
        root = Path(self.directory).resolve()
        target = (root / model / "settings.json").resolve()
        if root not in target.parents or not (target.parent / "model.json").exists():
            self.send_error(400, "unknown model")
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            require(0 < size <= 256 * 1024, 'settings size limit')
            data = json_bytes(self.rfile.read(size))
            require(isinstance(data, dict), 'settings must be an object')
            if 'playbackSettings' in data:
                model_data = json_bytes((target.parent / 'model.json').read_bytes())
                resolve_playback_settings(model_data.get('playbackSettings'), data['playbackSettings'])
        except (ValidationError, ValueError, TypeError, OSError) as exc:
            self.send_error(400, str(exc))
            return
        atomic_json(target, data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, fmt, *args):
        pass


def serve(root: Path, port: int = 8765):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(root)))
    print(f"serving {root} at http://127.0.0.1:{port}/editor.html")
    httpd.serve_forever()
