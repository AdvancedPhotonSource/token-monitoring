"""End-to-end proxy behaviour for streaming (SSE) responses."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from token_monitoring import proxy as proxy_mod
from token_monitoring.proxy import _SseUsageExtractor


# --------- unit test the SSE extractor first --------------------------------


def test_sse_extractor_captures_start_and_delta():
    ex = _SseUsageExtractor()
    events = [
        'event: message_start\n',
        'data: ' + json.dumps({
            "type": "message_start",
            "message": {"id": "m", "usage": {
                "input_tokens": 33,
                "output_tokens": 0,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 1,
            }},
        }) + '\n\n',
        'event: content_block_delta\n',
        'data: ' + json.dumps({"type": "content_block_delta", "delta": {"text": "hi"}}) + '\n\n',
        'event: message_delta\n',
        'data: ' + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                               "usage": {"output_tokens": 55}}) + '\n\n',
        'event: message_stop\n',
        'data: ' + json.dumps({"type": "message_stop"}) + '\n\n',
    ]
    for chunk in events:
        ex.feed(chunk.encode("utf-8"))
    ex.flush()
    assert ex.input_tokens == 33
    assert ex.output_tokens == 55
    assert ex.cache_read_tokens == 2
    assert ex.cache_creation_tokens == 1


def test_sse_extractor_handles_chunks_split_mid_line():
    ex = _SseUsageExtractor()
    data = ('data: ' + json.dumps({
        "type": "message_start",
        "message": {"usage": {"input_tokens": 9, "output_tokens": 0}},
    }) + '\n').encode()
    # Feed one byte at a time.
    for i in range(len(data)):
        ex.feed(data[i:i + 1])
    ex.flush()
    assert ex.input_tokens == 9


# --------- full-stack streaming proxy test ---------------------------------


SSE_BODY = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"m","usage":'
    b'{"input_tokens":100,"output_tokens":0,"cache_read_input_tokens":5}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"text":"chunk1"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","delta":{"text":"chunk2"}}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":77}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)


@pytest.fixture()
def app_with_streaming_upstream(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # stream=ByteStream so the proxy's stream=True + aiter_raw path works
        # (content= pre-buffers and marks the response as already-consumed).
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.ByteStream(SSE_BODY))

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy_mod, "_client", mock_client)

    from token_monitoring.api import build_app
    app = build_app()
    with TestClient(app) as tc:
        yield tc


def test_streaming_passthrough_and_row_written(app_with_streaming_upstream):
    client = app_with_streaming_upstream
    payload = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    with client.stream(
        "POST", "/v1/messages",
        headers={"x-api-key": "someone", "content-type": "application/json"},
        content=payload,
    ) as r:
        received = b""
        for chunk in r.iter_bytes():
            received += chunk
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")

    # Byte-for-byte passthrough.
    assert received == SSE_BODY

    # Row recorded with usage from SSE events.
    from token_monitoring import db as db_mod
    store = db_mod.store()
    assert store is not None
    rows = store.list_requests()
    assert len(rows) == 1
    row = rows[0]
    assert row["streamed"] == 1
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 77
    assert row["cache_read_tokens"] == 5
    assert row["model"] == "claude-haiku-4-5"
    assert row["status_code"] == 200
