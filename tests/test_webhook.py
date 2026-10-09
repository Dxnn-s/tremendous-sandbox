import hashlib
import hmac
import json
import urllib.error
import urllib.request

import pytest

from tremendous_sandbox import (
    SIGNATURE_HEADER,
    EventStore,
    WebhookEvent,
    WebhookReceiver,
    handle_delivery,
    sign,
    verify,
)
from tremendous_sandbox.fixtures import SAMPLE_WEBHOOK_EVENT

KEY = "jXjdU8dozx571TG6YzL4"
RAW = json.dumps(SAMPLE_WEBHOOK_EVENT).encode("utf-8")


def test_sign_matches_documented_format():
    expected = "sha256=" + hmac.new(KEY.encode(), RAW, hashlib.sha256).hexdigest()
    assert sign(KEY, RAW) == expected


def test_verify_accepts_a_valid_signature():
    assert verify(KEY, RAW, sign(KEY, RAW)) is True


def test_verify_rejects_a_tampered_body():
    tampered = RAW.replace(b"ORDERS.CREATED", b"ORDERS.APPROVED")
    assert verify(KEY, tampered, sign(KEY, RAW)) is False


def test_verify_rejects_a_reserialized_body():
    # Different separators change the bytes. The docs say to hash the raw body for this reason.
    reserialized = json.dumps(SAMPLE_WEBHOOK_EVENT, separators=(",", ":")).encode("utf-8")
    assert reserialized != RAW
    assert verify(KEY, reserialized, sign(KEY, RAW)) is False


@pytest.mark.parametrize("header", [None, "", "abc", "sha1=deadbeef", "sha256=", "sha256=00"])
def test_verify_rejects_missing_or_malformed_header(header):
    assert verify(KEY, RAW, header) is False


def test_verify_rejects_wrong_key():
    assert verify("other-key", RAW, sign(KEY, RAW)) is False


def test_event_parse_requires_uuid_and_event():
    event = WebhookEvent.from_bytes(RAW)
    assert event.uuid == SAMPLE_WEBHOOK_EVENT["uuid"]
    assert event.event == "ORDERS.CREATED"
    assert event.payload["resource"]["type"] == "orders"
    with pytest.raises(ValueError):
        WebhookEvent.from_bytes(b'{"event": "ORDERS.CREATED"}')


def test_store_dedupes_by_uuid(tmp_path):
    store = EventStore(str(tmp_path / "events.sqlite3"))
    event = WebhookEvent.from_bytes(RAW)
    assert store.record(event) is True
    assert store.record(event) is False
    assert store.count() == 1
    assert store.uuids() == [event.uuid]
    store.close()


def test_store_persists_across_connections(tmp_path):
    path = str(tmp_path / "events.sqlite3")
    first = EventStore(path)
    first.record(WebhookEvent.from_bytes(RAW))
    first.close()
    second = EventStore(path)
    assert second.record(WebhookEvent.from_bytes(RAW)) is False
    assert second.count() == 1
    second.close()


def test_handle_delivery_rejects_bad_signature_without_storing():
    store = EventStore()
    outcome = handle_delivery(KEY, store, RAW, "sha256=bad")
    assert outcome.status == 401
    assert outcome.reason == "bad_signature"
    assert store.count() == 0


def test_handle_delivery_accepts_then_ignores_duplicate():
    store = EventStore()
    first = handle_delivery(KEY, store, RAW, sign(KEY, RAW))
    second = handle_delivery(KEY, store, RAW, sign(KEY, RAW))
    assert (first.status, first.reason) == (200, "accepted")
    assert (second.status, second.reason) == (200, "duplicate")
    assert store.count() == 1


def test_handle_delivery_rejects_signed_but_malformed_payload():
    store = EventStore()
    raw = b'{"hello": "world"}'
    outcome = handle_delivery(KEY, store, raw, sign(KEY, raw))
    assert outcome.status == 400
    assert outcome.reason == "bad_payload"
    assert store.count() == 0


def _post(url: str, raw: bytes, signature: str | None) -> int:
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    req = urllib.request.Request(url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def test_receiver_end_to_end_over_http():
    store = EventStore()
    receiver = WebhookReceiver(KEY, store).start()
    try:
        assert _post(receiver.url, RAW, sign(KEY, RAW)) == 200
        assert _post(receiver.url, RAW.replace(b"CREATED", b"FAILED"), sign(KEY, RAW)) == 401
        assert _post(receiver.url, RAW, None) == 401
        assert _post(receiver.url, RAW, sign(KEY, RAW)) == 200
        assert store.count() == 1
        assert [o.reason for o in receiver.outcomes] == [
            "accepted",
            "bad_signature",
            "bad_signature",
            "duplicate",
        ]
    finally:
        receiver.stop()
        store.close()
