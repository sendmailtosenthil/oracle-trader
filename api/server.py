"""Standalone enctoken ingest API for Project Oracle.

A tiny, zero-dependency HTTP server (Python stdlib only) that lets an external
client — specifically the companion Chrome extension — push a fresh Kite
``enctoken`` (and ``user_id``) into the same ``oracle.db`` the Streamlit app
reads from. It exists as a separate process because Streamlit has no clean way
to expose a custom REST route.

It reuses the app's own persistence layer (:mod:`common.database`) so the token
lands in exactly the same ``broker_config`` row the UI writes to, and clears the
on-disk token-validity cache so the next page load re-checks immediately.

Auth: HTTP Basic. Credentials come from the environment
(``ENCTOKEN_API_USER`` / ``ENCTOKEN_API_PASS``); the server refuses to start
without them. Run it behind HTTPS in production — Basic auth + the enctoken are
sent in the request and are only as private as the transport.

Endpoints
    GET  /api/health    -> {"status": "ok"}                 (no auth)
    POST /api/enctoken  -> {"status": "success", ...}        (Basic auth)
        body: {"user_id": "PC8006", "enctoken": "<...>"}

Run:  python -m api.server            (from the repo root)
Env:  ENCTOKEN_API_USER, ENCTOKEN_API_PASS   (required)
      ENCTOKEN_API_PORT   (default 8502)
      ENCTOKEN_API_HOST   (default 0.0.0.0)
      ENCTOKEN_API_CORS_ORIGIN  (default https://kite.zerodha.com)
"""
import base64
import hmac
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common.database import init_db, get_db, BrokerConfig
from common.broker import clear_token_cache

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [enctoken-api] %(levelname)s %(message)s"
)
log = logging.getLogger("enctoken-api")

# Allowed origin for the browser preflight. The Chrome extension's service
# worker fetch (with host_permissions) is not subject to CORS, but a content
# script fetch would be — allowing kite.zerodha.com covers both paths.
_CORS_ORIGIN = os.environ.get("ENCTOKEN_API_CORS_ORIGIN", "https://kite.zerodha.com")

_API_USER = os.environ.get("ENCTOKEN_API_USER", "")
_API_PASS = os.environ.get("ENCTOKEN_API_PASS", "")


def _check_basic_auth(header):
    """Constant-time check of an ``Authorization: Basic ...`` header."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    # hmac.compare_digest on both fields to avoid leaking length/prefix via timing.
    return hmac.compare_digest(user, _API_USER) and hmac.compare_digest(password, _API_PASS)


class Handler(BaseHTTPRequestHandler):
    server_version = "OracleEnctokenAPI/1.0"

    # ----- helpers -----------------------------------------------------
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", _CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # route stdlib logging through our logger
        log.info("%s - %s", self.address_string(), fmt % args)

    # ----- routes ------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/api/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/api/enctoken":
            self._send_json(404, {"status": "error", "message": "not found"})
            return

        if not _check_basic_auth(self.headers.get("Authorization")):
            self._send_json(
                401,
                {"status": "error", "message": "unauthorized"},
                extra_headers={"WWW-Authenticate": 'Basic realm="oracle"'},
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            self._send_json(400, {"status": "error", "message": "invalid JSON body"})
            return

        enctoken = (data.get("enctoken") or "").strip()
        user_id = (data.get("user_id") or "").strip()
        if not enctoken:
            self._send_json(400, {"status": "error", "message": "enctoken is required"})
            return
        if not user_id:
            user_id = os.environ.get("ZERODHA_USER_ID", "PC8006")

        try:
            db = next(get_db())
            try:
                cfg = (
                    db.query(BrokerConfig)
                    .filter(BrokerConfig.broker_name == "ZERODHA")
                    .first()
                )
                if cfg:
                    cfg.user_id = user_id
                    cfg.enctoken = enctoken
                else:
                    db.add(
                        BrokerConfig(
                            broker_name="ZERODHA", user_id=user_id, enctoken=enctoken
                        )
                    )
                db.commit()
            finally:
                db.close()
            clear_token_cache()
        except Exception as exc:  # noqa: BLE001 - report failure to the caller
            log.exception("failed to persist enctoken")
            self._send_json(500, {"status": "error", "message": str(exc)})
            return

        log.info("enctoken updated for user_id=%s (len=%d)", user_id, len(enctoken))
        self._send_json(
            200,
            {"status": "success", "user_id": user_id, "enctoken_len": len(enctoken)},
        )


def main():
    if not _API_USER or not _API_PASS:
        log.error(
            "ENCTOKEN_API_USER and ENCTOKEN_API_PASS must be set — refusing to start."
        )
        sys.exit(1)

    # Ensure the schema exists and the shared SessionLocal is wired up before we
    # start serving (same DB the Streamlit app uses).
    init_db()

    host = os.environ.get("ENCTOKEN_API_HOST", "0.0.0.0")
    port = int(os.environ.get("ENCTOKEN_API_PORT", "8502"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    log.info("listening on %s:%d (CORS origin: %s)", host, port, _CORS_ORIGIN)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
