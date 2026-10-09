"""Recorded response shapes and an in-memory fake of the endpoints the client uses.

Every body here follows the example responses in the Tremendous reference docs.
The fake applies the documented external_id rules:

  first create                         200, order body
  same external_id, same parameters    201, the original order body
  same external_id, other parameters   409, documented error body

It never opens a socket. Tests and the offline replay inject it as the transport.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .http import Request, Response

SAMPLE_PRODUCT = {
    "id": "PRODUCTSAMPLE",
    "name": "Sample Gift Card",
    "category": "merchant_cards",
    "currency_codes": ["USD"],
    "countries": [{"abbr": "US"}],
}

SAMPLE_WEBHOOK_EVENT = {
    "event": "ORDERS.CREATED",
    "uuid": "5ccc7bb1-7659-4e23-a407-77d8cd9c62f5",
    "created_utc": "2021-04-06T20:05:01.037-04:00",
    "payload": {"resource": {"id": "2V3PCCL7QXDA", "type": "orders"}, "meta": {}},
}


def order_body(order_id: str, external_id: str | None, reward: dict[str, Any], created_at: str) -> dict[str, Any]:
    """Shape of the order object from the create and get order references."""
    value = reward.get("value", {})
    denomination = float(value.get("denomination", 0))
    fees = round(denomination * 0.05, 2)
    recipient = reward.get("recipient", {})
    delivery = reward.get("delivery", {"method": "EMAIL"})
    return {
        "id": order_id,
        "external_id": external_id,
        "campaign_id": reward.get("campaign_id"),
        "created_at": created_at,
        "status": "EXECUTED",
        "channel": "API",
        "payment": {
            "subtotal": denomination,
            "total": round(denomination + fees, 2),
            "fees": fees,
            "discount": 0,
            "currency_code": value.get("currency_code", "USD"),
        },
        "invoice_id": None,
        "rewards": [
            {
                "id": f"{order_id}R1",
                "order_id": order_id,
                "created_at": created_at,
                "expires_at": None,
                "value": {
                    "denomination": denomination,
                    "currency_code": value.get("currency_code", "USD"),
                },
                "recipient": {
                    "name": recipient.get("name"),
                    "email": recipient.get("email"),
                    "phone": recipient.get("phone"),
                },
                "deliver_at": reward.get("deliver_at"),
                "custom_fields": [],
                "delivery": {
                    "method": delivery.get("method", "EMAIL"),
                    "status": "SCHEDULED",
                    "link": f"https://testflight.tremendous.com/rewards/payout/{order_id.lower()}",
                },
            }
        ],
    }


def error_body(status: int, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shape of every documented error: {"status": N, "errors": {"message": ..., "payload": {...}}}."""
    return {"status": status, "errors": {"message": message, "payload": payload or {}}}


def _json(status: int, body: Any) -> Response:
    return Response(status, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})


class FakeTremendous:
    """Callable transport. Holds orders in memory and records every request it sees."""

    def __init__(self, webhook_private_key: str = "jXjdU8dozx571TG6YzL4") -> None:
        self.requests: list[Request] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self._by_external: dict[str, tuple[str, dict[str, Any]]] = {}
        self._counter = 0
        self.webhook: dict[str, str] | None = None
        self.webhook_private_key = webhook_private_key
        self.simulated: list[str] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) <= len("Bearer "):
            return _json(401, error_body(401, "Invalid API key"))
        parts = urlsplit(request.url)
        path = parts.path.split("/api/v2", 1)[-1]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(request.body) if request.body else {}

        if request.method == "POST" and path == "/orders":
            return self._create_order(body)
        if request.method == "GET" and path == "/orders":
            return self._list_orders(query)
        if request.method == "GET" and path.startswith("/orders/"):
            return self._get_order(path.rsplit("/", 1)[-1])
        if request.method == "GET" and path == "/products":
            return _json(200, {"products": [SAMPLE_PRODUCT]})
        if request.method == "POST" and path == "/webhooks":
            self.webhook = {"id": "WEBHOOKSAMPLE", "url": body["url"], "private_key": self.webhook_private_key}
            return _json(200, {"webhook": self.webhook})
        if request.method == "POST" and path.endswith("/simulate"):
            if self.webhook is None:
                return _json(404, error_body(404, "No resource could be found for the provided ID"))
            self.simulated.append(body["event"])
            return Response(200, b"")
        return _json(404, error_body(404, "No resource could be found for the provided ID"))

    def _create_order(self, body: dict[str, Any]) -> Response:
        external_id = body.get("external_id")
        if external_id is not None and external_id in self._by_external:
            order_id, original_body = self._by_external[external_id]
            if original_body == body:
                return _json(201, {"order": self.orders[order_id]})
            return _json(
                409,
                error_body(
                    409,
                    "An order with the same external_id already exists with different parameters",
                ),
            )
        self._counter += 1
        order_id = f"ORDER{self._counter:07d}"
        created_at = f"2026-10-09T00:00:{self._counter:02d}Z"
        order = order_body(order_id, external_id, body["reward"], created_at)
        self.orders[order_id] = order
        if external_id is not None:
            self._by_external[external_id] = (order_id, body)
        return _json(200, {"order": order})

    def _get_order(self, order_id: str) -> Response:
        order = self.orders.get(order_id)
        if order is None:
            return _json(404, error_body(404, "No resource could be found for the provided ID"))
        return _json(200, {"order": order})

    def _list_orders(self, query: dict[str, str]) -> Response:
        orders = list(self.orders.values())
        if "external_id" in query:
            orders = [o for o in orders if o["external_id"] == query["external_id"]]
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 10))
        return _json(200, {"orders": orders[offset : offset + limit], "total_count": len(orders)})
