import json

import pytest

from tremendous_sandbox import (
    SANDBOX_BASE_URL,
    ConflictError,
    NotFoundError,
    Request,
    Response,
    TremendousClient,
    TremendousError,
)
from tremendous_sandbox.fixtures import FakeTremendous, error_body

REWARD = {
    "value": {"denomination": 5, "currency_code": "USD"},
    "products": ["PRODUCTSAMPLE"],
    "recipient": {"name": "Replay Test", "email": "replay@example.com"},
    "delivery": {"method": "LINK"},
}


@pytest.fixture
def fake() -> FakeTremendous:
    return FakeTremendous()


@pytest.fixture
def client(fake: FakeTremendous) -> TremendousClient:
    return TremendousClient("TEST_fake_key", SANDBOX_BASE_URL, fake)


def test_create_order_sends_bearer_auth_and_json_body(client, fake):
    client.create_order("ext-1", REWARD)
    request = fake.requests[-1]
    assert request.method == "POST"
    assert request.url == f"{SANDBOX_BASE_URL}/orders"
    assert request.headers["Authorization"] == "Bearer TEST_fake_key"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.body)
    assert body["external_id"] == "ext-1"
    assert body["payment"] == {"funding_source_id": "BALANCE"}
    assert body["reward"] == REWARD


def test_first_create_returns_200_and_an_order(client):
    result = client.create_order("ext-1", REWARD)
    assert result.status_code == 200
    assert result.replayed is False
    assert result.order.external_id == "ext-1"
    assert result.order.status == "EXECUTED"
    assert result.order.raw["rewards"][0]["value"]["denomination"] == 5


def test_same_external_id_same_body_returns_201_with_the_original_order(client):
    first = client.create_order("ext-1", REWARD)
    second = client.create_order("ext-1", REWARD)
    assert second.status_code == 201
    assert second.replayed is True
    assert second.order.id == first.order.id
    assert second.order.raw == first.order.raw


def test_same_external_id_changed_body_raises_409(client):
    client.create_order("ext-1", REWARD)
    changed = dict(REWARD, value={"denomination": 6, "currency_code": "USD"})
    with pytest.raises(ConflictError) as excinfo:
        client.create_order("ext-1", changed)
    assert excinfo.value.status == 409
    assert excinfo.value.body["status"] == 409
    assert "external_id" in excinfo.value.body["errors"]["message"]


def test_replay_creates_exactly_one_order(client):
    client.create_order("ext-1", REWARD)
    client.create_order("ext-1", REWARD)
    page = client.list_orders(external_id="ext-1")
    assert page.total_count == 1
    assert len(page.orders) == 1


def test_get_order_returns_the_order(client):
    created = client.create_order("ext-1", REWARD)
    fetched = client.get_order(created.order.id)
    assert fetched.id == created.order.id
    assert fetched.raw == created.order.raw


def test_get_order_missing_raises_404(client):
    with pytest.raises(NotFoundError) as excinfo:
        client.get_order("NOPE")
    assert excinfo.value.status == 404
    assert excinfo.value.body["errors"]["message"] == "No resource could be found for the provided ID"


def test_list_orders_passes_query_params(client, fake):
    client.create_order("ext-1", REWARD)
    client.create_order("ext-2", REWARD)
    page = client.list_orders(limit=1, offset=1)
    request = fake.requests[-1]
    assert request.method == "GET"
    assert request.url.startswith(f"{SANDBOX_BASE_URL}/orders?")
    assert "limit=1" in request.url and "offset=1" in request.url
    assert page.total_count == 2
    assert len(page.orders) == 1


def test_unauthorized_response_raises_401():
    def transport(request: Request) -> Response:
        return Response(401, json.dumps(error_body(401, "Invalid API key")).encode())

    client = TremendousClient("TEST_bad", SANDBOX_BASE_URL, transport)
    with pytest.raises(TremendousError) as excinfo:
        client.get_order("ORDER0000001")
    assert excinfo.value.status == 401
    assert "Invalid API key" in str(excinfo.value)


def test_empty_api_key_is_rejected_up_front():
    with pytest.raises(ValueError):
        TremendousClient("")


def test_register_webhook_returns_private_key(client):
    webhook = client.register_webhook("https://example.com/webhook")
    assert webhook.id == "WEBHOOKSAMPLE"
    assert webhook.url == "https://example.com/webhook"
    assert webhook.private_key == "jXjdU8dozx571TG6YzL4"


def test_simulate_webhook_posts_event_name(client, fake):
    webhook = client.register_webhook("https://example.com/webhook")
    client.simulate_webhook(webhook.id, "ORDERS.CREATED")
    assert fake.simulated == ["ORDERS.CREATED"]
    assert fake.requests[-1].url.endswith("/webhooks/WEBHOOKSAMPLE/simulate")
