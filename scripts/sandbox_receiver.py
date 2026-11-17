"""Local sandbox SaaS substitute: verify signatures and persist/deduplicate received events."""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATABASE = os.getenv("SANDBOX_RECEIVER_DB", "/data/events.sqlite3")


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/events/":
            with sqlite3.connect(DATABASE) as db:
                events = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT body FROM events ORDER BY received_at DESC, rowid DESC LIMIT 100"
                    )
                ]
            self.reply(200, json.dumps(events).encode())
        elif self.path in {"/", "/return/"}:
            self.reply(
                200,
                (
                    '<!doctype html><html lang="es"><meta charset="utf-8">'
                    "<title>Prueba local de SumUp</title>"
                    '<body style="font:18px system-ui;max-width:720px;'
                    'margin:60px auto;padding:20px">'
                    "<h1>SaaS local de pruebas</h1>"
                    "<p>Este retorno del navegador no confirma el pago. "
                    "La herramienta lo comprueba consultando a SumUp.</p>"
                    '<p><a href="http://localhost:8000/admin/">Abrir el panel de pagos</a></p>'
                    '<p><a href="/events/">Ver las notificaciones recibidas y verificadas</a></p>'
                    "<p>Los eventos se conservan y se deduplican por identificador.</p>"
                    "</body></html>"
                ).encode(),
                "text/html; charset=utf-8",
            )
        else:
            self.reply(404)

    def do_POST(self):
        if self.path != "/notifications/":
            self.reply(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                raise ValueError
            body = self.rfile.read(length)
            timestamp = self.headers.get("X-Webhook-Timestamp", "")
            if abs(time.time() - int(timestamp)) > 300:
                raise ValueError
            expected = (
                "v1="
                + hmac.new(
                    os.environ["SAAS_WEBHOOK_SECRET"].encode(),
                    timestamp.encode() + b"." + body,
                    hashlib.sha256,
                ).hexdigest()
            )
            if not hmac.compare_digest(expected, self.headers.get("X-Webhook-Signature", "")):
                self.reply(401)
                return
            event = json.loads(body)
            event_id = self.headers.get("X-Webhook-Id", "")
            if not event_id or event.get("id") != event_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            self.reply(400)
            return
        with sqlite3.connect(DATABASE) as db:
            existing = db.execute("SELECT body FROM events WHERE id=?", (event_id,)).fetchone()
            if existing and existing[0] != body.decode():
                self.reply(409)
                return
            db.execute(
                "INSERT OR IGNORE INTO events(id, body, received_at) VALUES (?, ?, ?)",
                (event_id, body.decode(), time.time()),
            )
        self.reply(204)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    if len(os.getenv("SAAS_WEBHOOK_SECRET", "")) < 32:
        raise SystemExit("Configura SAAS_WEBHOOK_SECRET para el receptor local.")
    with sqlite3.connect(DATABASE) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS events "
            "(id TEXT PRIMARY KEY, body TEXT NOT NULL, received_at REAL NOT NULL)"
        )
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
