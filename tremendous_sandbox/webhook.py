"""Webhook signature verification, uuid deduplication and a stdlib receiver.

Reference: https://developers.tremendous.com/docs/webhooks-1

The header is Tremendous-Webhook-Signature. Its value is the prefix "sha256="
followed by the hex HMAC-SHA256 of the raw request body, keyed with the
private_key returned when the webhook was registered. The docs say to verify
against the raw body, never a re-serialized one, and to check the uuid on every
delivery because failed deliveries are retried up to 17 times with exponential
backoff.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

SIGNATURE_HEADER = "Tremendous-Webhook-Signature"
SIGNATURE_PREFIX = "sha256="


def sign(private_key: str, raw_body: bytes) -> str:
    digest = hmac.new(private_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify(private_key: str, raw_body: bytes, header_value: str | None) -> bool:
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(sign(private_key, raw_body), header_value)


@dataclass(frozen=True)
class WebhookEvent:
    uuid: str
    event: str
    created_utc: str | None
    payload: dict[str, Any]
    raw: bytes

    @classmethod
    def from_bytes(cls, raw: bytes) -> "WebhookEvent":
        data = json.loads(raw)
        if not isinstance(data, dict) or "uuid" not in data or "event" not in data:
            raise ValueError("webhook body must be an object with uuid and event")
        return cls(
            uuid=str(data["uuid"]),
            event=str(data["event"]),
            created_utc=data.get("created_utc"),
            payload=dict(data.get("payload") or {}),
            raw=raw,
        )


class EventStore:
    """Accepted deliveries, one row per uuid. The primary key is the dedupe."""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    uuid        TEXT PRIMARY KEY,
                    event       TEXT NOT NULL,
                    created_utc TEXT,
                    body        TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def record(self, event: WebhookEvent) -> bool:
        """Insert the event. Returns True if it was new, False if the uuid was already stored."""
        received_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO webhook_events (uuid, event, created_utc, body, received_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event.uuid,
                    event.event,
                    event.created_utc,
                    event.raw.decode("utf-8"),
                    received_at,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0])

    def uuids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT uuid FROM webhook_events ORDER BY received_at, rowid"
            ).fetchall()
        return [row[0] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


@dataclass(frozen=True)
class Outcome:
    status: int
    reason: str  # accepted, duplicate, bad_signature, bad_payload
    uuid: str | None = None


def handle_delivery(
    private_key: str,
    store: EventStore,
    raw_body: bytes,
    signature_header: str | None,
) -> Outcome:
    """The whole receiver policy in one place so it can be tested without a socket.

    Bad signature: 401, nothing stored. Tremendous will retry, which is fine
    because a retry of a genuine event will carry a valid signature.
    Duplicate uuid: 200, nothing stored. Returning 200 stops the retry loop.
    Accepted: 200, one row written.
    """
    if not verify(private_key, raw_body, signature_header):
        return Outcome(401, "bad_signature")
    try:
        event = WebhookEvent.from_bytes(raw_body)
    except ValueError:
        return Outcome(400, "bad_payload")
    if store.record(event):
        return Outcome(200, "accepted", event.uuid)
    return Outcome(200, "duplicate", event.uuid)


class WebhookReceiver:
    """Single-threaded HTTPServer on a background thread. Enough for one endpoint."""

    def __init__(
        self,
        private_key: str,
        store: EventStore,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.private_key = private_key
        self.store = store
        self.outcomes: list[Outcome] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                outcome = handle_delivery(
                    receiver.private_key,
                    receiver.store,
                    raw,
                    self.headers.get(SIGNATURE_HEADER),
                )
                receiver.outcomes.append(outcome)
                body = json.dumps({"result": outcome.reason}).encode("utf-8")
                self.send_response(outcome.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self._server = HTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/webhook"

    def start(self) -> "WebhookReceiver":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
