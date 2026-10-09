"""Replay demo for the Tremendous order and webhook path.

Offline (default): no key, no network. Uses the recorded fixtures in
tremendous_sandbox/fixtures.py and a local webhook receiver.

    python replay.py

Live orders against the sandbox (needs TREMENDOUS_API_KEY):

    python replay.py --live

Live webhook delivery as well (needs the key plus a public URL, for example an
ngrok tunnel, that forwards to --port on this machine):

    python replay.py --live --register-url https://<your-tunnel>/webhook --port 8000

The script demonstrates three things and prints each result:

  1. The same external_id sent twice yields one order. The second response is
     201 with the same order id. A third call with one changed field is 409.
  2. A webhook with a tampered body fails the Tremendous-Webhook-Signature check.
  3. A webhook delivered twice with the same uuid is stored once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from tremendous_sandbox import (
    SANDBOX_BASE_URL,
    SIGNATURE_HEADER,
    ConflictError,
    EventStore,
    TremendousClient,
    WebhookReceiver,
    sign,
    urllib_transport,
)
from tremendous_sandbox.fixtures import SAMPLE_WEBHOOK_EVENT, FakeTremendous

OFFLINE_PRIVATE_KEY = "offline-webhook-private-key"
QUOTES = "\"'"


def load_dotenv(path: Path) -> None:
    """Tiny .env reader. Does not override variables already in the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(QUOTES))


def build_reward(product_id: str, denomination: float) -> dict[str, Any]:
    return {
        "value": {"denomination": denomination, "currency_code": "USD"},
        "products": [product_id],
        "recipient": {"name": "Replay Test", "email": "replay@example.com"},
        "delivery": {"method": "LINK"},
    }


def post_webhook(url: str, raw_body: bytes, signature: str) -> int:
    req = urllib.request.Request(
        url,
        data=raw_body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def demo_orders(client: TremendousClient, product_id: str) -> list[str]:
    lines: list[str] = []
    external_id = f"replay-{uuid.uuid4()}"
    reward = build_reward(product_id, 5)

    first = client.create_order(external_id, reward)
    lines.append(
        f"create #1  external_id={external_id}  status={first.status_code}  order={first.order.id}"
    )

    second = client.create_order(external_id, reward)
    lines.append(
        f"create #2  same body              status={second.status_code}  order={second.order.id}"
        f"  same_order={second.order.id == first.order.id}  replayed={second.replayed}"
    )
    assert second.order.id == first.order.id, "second call must return the original order"
    assert second.status_code == 201, "documented replay status is 201"

    changed = build_reward(product_id, 6)
    try:
        client.create_order(external_id, changed)
        raise AssertionError("expected 409 for a changed body under the same external_id")
    except ConflictError as err:
        lines.append(f"create #3  denomination changed    status={err.status}  conflict")

    page = client.list_orders(external_id=external_id)
    fetched = client.get_order(first.order.id)
    lines.append(
        f"list_orders(external_id) total_count={page.total_count}  get_order status={fetched.status}"
    )
    assert page.total_count == 1
    return lines


def demo_webhooks_local(private_key: str, receiver_url: str, store: EventStore) -> list[str]:
    lines: list[str] = []
    event = dict(SAMPLE_WEBHOOK_EVENT, uuid=str(uuid.uuid4()))
    raw = json.dumps(event).encode("utf-8")
    good = sign(private_key, raw)
    before = store.count()

    status = post_webhook(receiver_url, raw, good)
    lines.append(f"delivery #1  valid signature      http={status}  stored={store.count()}")
    assert status == 200 and store.count() == before + 1

    tampered = raw.replace(b"ORDERS.CREATED", b"ORDERS.APPROVED")
    status = post_webhook(receiver_url, tampered, good)
    lines.append(f"delivery #2  body tampered        http={status}  stored={store.count()}")
    assert status == 401 and store.count() == before + 1

    status = post_webhook(receiver_url, raw, good)
    lines.append(f"delivery #3  same uuid again      http={status}  stored={store.count()}")
    assert status == 200 and store.count() == before + 1
    return lines


def demo_webhooks_live(
    client: TremendousClient, register_url: str, port: int, db_path: str
) -> list[str]:
    lines: list[str] = []
    webhook = client.register_webhook(register_url)
    lines.append(f"registered webhook id={webhook.id} url={webhook.url}")
    store = EventStore(db_path)
    receiver = WebhookReceiver(webhook.private_key, store, host="0.0.0.0", port=port).start()
    try:
        client.simulate_webhook(webhook.id, "ORDERS.CREATED")
        deadline = time.time() + 30
        while time.time() < deadline and store.count() == 0:
            time.sleep(0.5)
        if store.count() == 0:
            lines.append(
                "no delivery arrived within 30 seconds; check the tunnel forwards to this port"
            )
            return lines
        lines.append(
            f"simulate ORDERS.CREATED  accepted uuid={store.uuids()[-1]}  stored={store.count()}"
        )
        lines.extend(
            demo_webhooks_local(webhook.private_key, f"http://127.0.0.1:{port}/webhook", store)
        )
    finally:
        receiver.stop()
        store.close()
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the order calls against testflight (needs TREMENDOUS_API_KEY)",
    )
    parser.add_argument(
        "--register-url", help="public URL to register as the webhook endpoint (live only)"
    )
    parser.add_argument("--port", type=int, default=8000, help="local port for the live receiver")
    parser.add_argument(
        "--db", default="webhook_events.sqlite3", help="SQLite path for the live receiver"
    )
    args = parser.parse_args(argv)

    if args.live:
        load_dotenv(Path(__file__).with_name(".env"))
        api_key = os.environ.get("TREMENDOUS_API_KEY", "")
        if not api_key:
            print(
                "TREMENDOUS_API_KEY is not set. Copy .env.example to .env and add your sandbox key.",
                file=sys.stderr,
            )
            return 2
        client = TremendousClient(api_key, SANDBOX_BASE_URL, urllib_transport)
        products = client.list_products()
        usd = [p for p in products if "USD" in p.get("currency_codes", [])]
        product_id = (usd or products)[0]["id"]
        mode = "live sandbox"
    else:
        client = TremendousClient("TEST_offline", SANDBOX_BASE_URL, FakeTremendous())
        product_id = client.list_products()[0]["id"]
        mode = "offline fixtures"

    print(f"[orders: {mode}]")
    for line in demo_orders(client, product_id):
        print("  " + line)

    if args.live and args.register_url:
        print(f"[webhooks: live, receiver on port {args.port}]")
        for line in demo_webhooks_live(client, args.register_url, args.port, args.db):
            print("  " + line)
    else:
        print("[webhooks: local receiver, signed with a local key]")
        store = EventStore(":memory:")
        receiver = WebhookReceiver(OFFLINE_PRIVATE_KEY, store).start()
        try:
            for line in demo_webhooks_local(OFFLINE_PRIVATE_KEY, receiver.url, store):
                print("  " + line)
        finally:
            receiver.stop()
            store.close()

    print("all three behaviors held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
