"""End-to-end proxy behaviour for non-streaming Anthropic responses."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from token_monitoring import proxy as proxy_mod


@pytest.fixture()
def app_with_mock_upstream(monkeypatch):
    """Build the FastAPI app with a mock upstream via httpx.MockTransport."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        body = json.dumps({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 42,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 1,
            },
        }).encode()
        # stream=ByteStream so the proxy's stream=True + aiter_raw path works
        # (content= pre-buffers and marks the response as already-consumed).
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=httpx.ByteStream(body))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy_mod, "_client", mock_client)

    from token_monitoring.api import build_app
    app = build_app()
    # TestClient as context manager triggers lifespan, which init_db()'s the store.
    with TestClient(app) as tc:
        yield tc, captured


def test_forwards_and_records_usage(app_with_mock_upstream):
    client, captured = app_with_mock_upstream

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = client.post(
        "/v1/messages",
        headers={"x-api-key": "haskels", "content-type": "application/json"},
        content=json.dumps(payload).encode(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["usage"]["input_tokens"] == 42

    # The upstream got the same body + the API key header.
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"].get("x-api-key") == "haskels"
    assert json.loads(captured["body"])["model"] == "claude-sonnet-4-6"
    # We must force identity encoding upstream so the SSE tee sees text,
    # not gzipped bytes — httpx would otherwise inject a compressed default.
    assert captured["headers"].get("accept-encoding") == "identity"

    # And exactly one row landed in SQLite with the right numbers.
    from token_monitoring import db as db_mod
    store = db_mod.store()
    assert store is not None
    rows = store.list_requests()
    assert len(rows) == 1
    row = rows[0]
    assert row["input_tokens"] == 42
    assert row["output_tokens"] == 7
    assert row["cache_read_tokens"] == 3
    assert row["cache_creation_tokens"] == 1
    assert row["model"] == "claude-sonnet-4-6"
    assert row["status_code"] == 200
    assert row["streamed"] == 0
    assert row["user_hash"] == hashlib.sha256(b"haskels").hexdigest()
