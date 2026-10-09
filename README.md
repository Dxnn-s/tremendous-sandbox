# tremendous-sandbox

A small Python project that exercises the two parts of the Tremendous API that matter most when money moves: idempotent order creation through `external_id`, and signed, retried webhooks deduplicated by `uuid`. It runs fully offline against recorded response shapes taken from the public reference docs, and it can run against the Tremendous sandbox (testflight) once an API key is present.

Plain Python 3.11 or newer, standard library only. pytest is the one development dependency.

## What it demonstrates

1. **The same `external_id` sent twice yields one order.** The first `POST /orders` returns `200` and the new order. The second call with an identical body returns `201` and the same order object, byte for byte, with the same `id`. A third call with the same `external_id` but one changed field (the denomination) returns `409`. Those three codes come straight from the create order reference: `200` is "created order for a single reward, that got processed successfully", `201` is "an order with the same external_id already exists; the response payload will be the same as when the order was initially created", and `409` is "an order with the same external_id already exists, but you are now submitting different parameters". The replay script asserts all three and `list_orders(external_id=...)` reports `total_count == 1`.

2. **A tampered webhook is rejected.** Tremendous signs every delivery with the header `Tremendous-Webhook-Signature`, whose value is `sha256=` followed by the hex HMAC-SHA256 of the raw request body, keyed with the `private_key` returned when the webhook was registered. The receiver recomputes that over the raw bytes and compares with `hmac.compare_digest`. Changing one byte of the body, dropping the header, using the wrong prefix, or re-serializing the JSON all fail. A failed check returns `401` and stores nothing.

3. **A duplicate webhook is ignored.** Failed deliveries are retried up to 17 times with exponential backoff, so the same event can arrive more than once. Every accepted event is written to a SQLite table keyed on the `uuid` from the payload. A second delivery with the same `uuid` returns `200` (so Tremendous stops retrying) but writes no new row.

## Run it offline

No key, no network. The fake transport in `tremendous_sandbox/fixtures.py` applies the documented `external_id` rules and returns bodies shaped like the reference examples. The webhook half starts a local `http.server` receiver and signs deliveries with a local key.

```
pip install -r requirements.txt
python -m pytest
python replay.py
```

Expected output from `python replay.py`:

```
[orders: offline fixtures]
  create #1  external_id=replay-...  status=200  order=ORDER0000001
  create #2  same body              status=201  order=ORDER0000001  same_order=True  replayed=True
  create #3  denomination changed    status=409  conflict
  list_orders(external_id) total_count=1  get_order status=EXECUTED
[webhooks: local receiver, signed with a local key]
  delivery #1  valid signature      http=200  stored=1
  delivery #2  body tampered        http=401  stored=1
  delivery #3  same uuid again      http=200  stored=1
all three behaviors held
```

## Run it against the sandbox (needs a key)

These are the only commands that need a key. Nothing else in the repo makes a network call.

1. Create a sandbox account at https://app.testflight.tremendous.com/auth/signup. It is self serve, free, and comes with a pre-funded test balance that refills daily.
2. In the sandbox dashboard go to Team Settings, then Developers, then Generate API key. Sandbox keys start with `TEST_`.
3. Copy `.env.example` to `.env` and paste the key as `TREMENDOUS_API_KEY`. The file is ignored by git.
4. Run the order half live:

```
python replay.py --live
```

This picks the first USD product from `GET /products`, creates a five dollar order with a fresh `external_id`, replays it, changes the denomination, and prints the three status codes the sandbox actually returns. The webhook half still runs locally in this mode because the signature check and the dedupe are pure functions of the body, the key and the uuid, and a real delivery would need a public URL.

To also receive a real delivery from Tremendous, expose a local port with a tunnel such as ngrok and register it. This registers the organization's single webhook endpoint, starts the receiver on `--port`, calls `POST /webhooks/{id}/simulate` with `ORDERS.CREATED`, waits up to 30 seconds for the delivery, verifies the signature with the `private_key` from registration, and then runs the tampered and duplicate checks against the same store:

```
ngrok http 8000
python replay.py --live --register-url https://<your-tunnel>/webhook --port 8000
```

Accepted events land in `webhook_events.sqlite3` in the working directory. It is ignored by git.

## Layout

```
tremendous_sandbox/http.py       Request and Response dataclasses, the urllib transport
tremendous_sandbox/client.py     TremendousClient: create_order, get_order, list_orders,
                                 list_products, register_webhook, simulate_webhook
tremendous_sandbox/webhook.py    sign, verify, WebhookEvent, EventStore (SQLite),
                                 handle_delivery, WebhookReceiver (http.server)
tremendous_sandbox/fixtures.py   recorded response shapes and the FakeTremendous transport
replay.py                        the demo script, offline by default
tests/                           32 pytest tests, all offline
```

The client takes a `transport` callable of type `Request -> Response`. Tests and the offline replay pass `FakeTremendous()`; the live path passes `urllib_transport`. That is the whole injection seam.

## Notes from reading the docs

- The first create is `200`, not `201`. The `201` is reserved for the replay. Most APIs do it the other way around, so a client that treats `201` as "new" would miscount.
- The `409` decision is "different parameters", and the docs do not say which fields count. The fake compares the whole request body. The live run will show whether, for example, a changed recipient name alone triggers it.
- Each organization gets exactly one webhook endpoint, and registering again replaces the URL. Staging and production therefore need either two organizations or one receiver that routes on the event payload.
- The signature covers the raw body. Any framework that parses and re-serializes JSON before the handler sees it will break verification, which is why the receiver reads `rfile` directly.
- The receiver returns `401` on a bad signature rather than `200`. That means a forged delivery is retried by whoever sent it, which is harmless, and a genuine delivery with a transient key mismatch gets another chance.
- Webhook payloads carry only the resource `id` and `type`, not the resource itself. A real integration would follow up with `GET /orders/{id}`, which also means the dedupe should happen before that fetch.

## License

MIT. See `LICENSE`.
