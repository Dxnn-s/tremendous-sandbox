"""Thin client for the parts of the Tremendous API v2 this project exercises.

Reference: https://developers.tremendous.com/reference/create-order
           https://developers.tremendous.com/reference/get-order
           https://developers.tremendous.com/reference/list-orders
           https://developers.tremendous.com/reference/create-webhook
           https://developers.tremendous.com/reference/simulate-webhook
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from .http import Request, Response, Transport, urllib_transport

SANDBOX_BASE_URL = "https://testflight.tremendous.com/api/v2"
PRODUCTION_BASE_URL = "https://api.tremendous.com/api/v2"


class TremendousError(Exception):
    """Any 4xx or 5xx. Carries the documented error body: {"status": N, "errors": {...}}."""

    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        message = ""
        if isinstance(body, dict):
            message = str(body.get("errors", {}).get("message", ""))
        super().__init__(f"HTTP {status}: {message or body}")


class ConflictError(TremendousError):
    """409: the external_id exists but the parameters differ from the original order."""


class NotFoundError(TremendousError):
    """404: no resource for the given id."""


@dataclass(frozen=True)
class Order:
    id: str
    external_id: str | None
    status: str
    created_at: str
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Order":
        return cls(
            id=data["id"],
            external_id=data.get("external_id"),
            status=data["status"],
            created_at=data["created_at"],
            raw=data,
        )


@dataclass(frozen=True)
class OrderResult:
    """A create response. 200 means created now, 201 means an existing order was returned."""

    status_code: int
    order: Order

    @property
    def replayed(self) -> bool:
        return self.status_code == 201


@dataclass(frozen=True)
class OrderPage:
    orders: list[Order]
    total_count: int


@dataclass(frozen=True)
class Webhook:
    id: str
    url: str
    private_key: str


class TremendousClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = SANDBOX_BASE_URL,
        transport: Transport = urllib_transport,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Response:
        url = f"{self.base_url}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urlencode(query)}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        response = self.transport(Request(method, url, headers, data))
        if response.status >= 400:
            payload = _safe_json(response)
            if response.status == 409:
                raise ConflictError(response.status, payload)
            if response.status == 404:
                raise NotFoundError(response.status, payload)
            raise TremendousError(response.status, payload)
        return response

    def create_order(
        self,
        external_id: str,
        reward: dict[str, Any],
        funding_source_id: str = "BALANCE",
    ) -> OrderResult:
        """POST /orders. external_id makes the call idempotent.

        Documented outcomes:
          200  created now
          201  an order with this external_id already exists; body is the original order
          409  same external_id, different parameters (raised as ConflictError)
        """
        body = {
            "external_id": external_id,
            "payment": {"funding_source_id": funding_source_id},
            "reward": reward,
        }
        response = self._request("POST", "/orders", body=body)
        return OrderResult(response.status, Order.from_api(response.json()["order"]))

    def get_order(self, order_id: str) -> Order:
        response = self._request("GET", f"/orders/{order_id}")
        return Order.from_api(response.json()["order"])

    def list_orders(
        self,
        external_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> OrderPage:
        response = self._request(
            "GET",
            "/orders",
            params={"external_id": external_id, "limit": limit, "offset": offset},
        )
        data = response.json()
        return OrderPage(
            orders=[Order.from_api(item) for item in data["orders"]],
            total_count=int(data["total_count"]),
        )

    def list_products(self) -> list[dict[str, Any]]:
        return list(self._request("GET", "/products").json()["products"])

    def register_webhook(self, url: str) -> Webhook:
        """POST /webhooks. One endpoint per organization; calling again replaces the URL."""
        data = self._request("POST", "/webhooks", body={"url": url}).json()["webhook"]
        return Webhook(id=data["id"], url=data["url"], private_key=data["private_key"])

    def simulate_webhook(self, webhook_id: str, event: str) -> None:
        self._request("POST", f"/webhooks/{webhook_id}/simulate", body={"event": event})


def _safe_json(response: Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.body.decode("utf-8", errors="replace")
