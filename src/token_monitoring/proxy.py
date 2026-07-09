"""Anthropic Messages API proxy → Argo, with per-request token accounting.

Client shape (Anthropic SDK / VS Code plugins):
    POST /v1/messages
    x-api-key: <ANL username>          (Argo's current auth)
    Content-Type: application/json
    body: {"model": "...", "messages": [...], "stream": true, ...}

We forward every byte verbatim to `${TM_UPSTREAM_URL}${path}` and stream
the response back. Along the way we:

  1. hash the caller's key → `user_hash` (SHA-256 of x-api-key, or
     Authorization if x-api-key is absent) so the row is grouped by user
     without storing anything secret;
  2. parse the request body's `model` and `stream` fields for logging;
  3. tee the response bytes into a small SSE parser that pulls token
     counts out of `message_start` / `message_delta` events (streaming)
     or the top-level `usage` block (non-streaming);
  4. INSERT one row into `requests` once the upstream response is fully
     drained (success or failure).

We don't try to be clever about hop-by-hop headers beyond what the
upstream / client actually reject — `httpx` handles most of that.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from token_monitoring import config, db

_log = logging.getLogger(__name__)

# Headers we never forward — hop-by-hop or set by the transport.
_HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
    # httpx sets its own; letting them through can duplicate.
    "accept-encoding",
})

# Response headers we strip before returning to the client. Upstream may
# set Content-Length even when the body is chunked in our reply; drop it
# so Starlette computes the correct one (or omits it for streams).
_RESP_STRIP = frozenset({
    "content-length", "content-encoding", "transfer-encoding",
    "connection", "keep-alive",
})


def _hash_key(request: Request) -> str:
    """Derive a stable per-user id from whichever auth header the caller sent.

    Anthropic SDK uses `x-api-key`; some clients use `Authorization: Bearer`.
    Fall back to `"anonymous"` if neither is present (should never happen
    against Argo, which requires a key)."""
    key = request.headers.get("x-api-key") or ""
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        else:
            key = auth.strip()
    if not key:
        return "anonymous"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _peek_body(body: bytes) -> tuple[str, bool]:
    """Best-effort extract (model, stream_flag) from the request body.

    Failing to parse is fine — return sensible defaults. We don't want a
    weird body to break the forward path; Argo will reject it if it's
    actually bad."""
    if not body:
        return "unknown", False
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "unknown", False
    if not isinstance(parsed, dict):
        return "unknown", False
    model = str(parsed.get("model") or "unknown")
    stream = bool(parsed.get("stream"))
    return model, stream


class _SseUsageExtractor:
    """Streaming SSE parser. Feed it bytes; it accumulates the final
    per-message usage fields from Anthropic's event stream.

    Anthropic emits (relevant events only)::

        event: message_start
        data: {"type":"message_start","message":{...,"usage":{"input_tokens":N,"output_tokens":M,
                                                              "cache_read_input_tokens":R,
                                                              "cache_creation_input_tokens":C}}}

        event: message_delta
        data: {"type":"message_delta","delta":{...},"usage":{"output_tokens":N}}

    We take input/cache counts from message_start and OVERWRITE
    output_tokens with each message_delta (they're cumulative in
    Anthropic's spec, so the last one wins).
    """

    def __init__(self) -> None:
        self.input_tokens: Optional[int] = None
        self.output_tokens: Optional[int] = None
        self.cache_read_tokens: Optional[int] = None
        self.cache_creation_tokens: Optional[int] = None
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        # Split on newlines but keep the incomplete tail for next feed().
        # Anthropic emits `\n\n` between events; parsing line-by-line is fine
        # because we only care about `data:` lines.
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            self._handle_line(line)

    def flush(self) -> None:
        # Drain any trailing line (no newline terminator).
        if self._buf:
            self._handle_line(bytes(self._buf))
            self._buf.clear()

    def _handle_line(self, line: bytes) -> None:
        # Only data: lines carry JSON we care about.
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            return
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        etype = payload.get("type")
        if etype == "message_start":
            usage = (payload.get("message") or {}).get("usage") or {}
        elif etype == "message_delta":
            usage = payload.get("usage") or {}
        else:
            return
        if not isinstance(usage, dict):
            return
        if "input_tokens" in usage and usage["input_tokens"] is not None:
            self.input_tokens = int(usage["input_tokens"])
        if "output_tokens" in usage and usage["output_tokens"] is not None:
            self.output_tokens = int(usage["output_tokens"])
        if usage.get("cache_read_input_tokens") is not None:
            self.cache_read_tokens = int(usage["cache_read_input_tokens"])
        if usage.get("cache_creation_input_tokens") is not None:
            self.cache_creation_tokens = int(usage["cache_creation_input_tokens"])


def _extract_usage_json(body: bytes) -> tuple[Optional[int], Optional[int],
                                              Optional[int], Optional[int]]:
    """Pull usage out of a non-streaming Anthropic response body."""
    if not body:
        return None, None, None, None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None, None, None, None
    if not isinstance(parsed, dict):
        return None, None, None, None
    usage = parsed.get("usage") or {}
    if not isinstance(usage, dict):
        return None, None, None, None
    it = usage.get("input_tokens")
    ot = usage.get("output_tokens")
    cr = usage.get("cache_read_input_tokens")
    cc = usage.get("cache_creation_input_tokens")
    return (
        int(it) if it is not None else None,
        int(ot) if ot is not None else None,
        int(cr) if cr is not None else None,
        int(cc) if cc is not None else None,
    )


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _record(
    *,
    ts_utc: str,
    user_hash: str,
    model: str,
    endpoint: str,
    streamed: bool,
    status_code: int,
    latency_ms: int,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_read_tokens: Optional[int],
    cache_creation_tokens: Optional[int],
) -> None:
    store = db.store()
    if store is None:
        _log.warning("usage store not initialised; dropping request row")
        return
    try:
        store.insert_request(db.UsageRow(
            ts_utc=ts_utc,
            user_hash=user_hash,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            latency_ms=latency_ms,
            status_code=status_code,
            endpoint=endpoint,
            streamed=streamed,
        ))
    except Exception as exc:  # noqa: BLE001
        _log.error("failed to insert request row: %s", exc)


# =========================================================================
# HTTP client — one shared AsyncClient reused across requests
# =========================================================================


_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.upstream_timeout_s(),
                                  connect=10.0, read=None, write=30.0),
            follow_redirects=False,
        )
    return _client


async def shutdown_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


# =========================================================================
# Router
# =========================================================================


def build_router() -> APIRouter:
    router = APIRouter(tags=["proxy"])

    @router.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def proxy(path: str, request: Request) -> Response:
        endpoint = "/v1/" + path
        upstream = f"{config.upstream_url()}{endpoint}"
        body = await request.body()
        model, streamed_req = _peek_body(body)

        # Forward the client's headers verbatim except hop-by-hop.
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        # Force identity encoding upstream. httpx would otherwise inject
        # its own `Accept-Encoding: gzip,...` default, Argo would return a
        # gzipped body, and our SSE tee (which sees raw bytes) would parse
        # garbage — plus the client would receive gzip bytes with the
        # content-encoding header stripped by us.
        fwd_headers["accept-encoding"] = "identity"

        user_hash = _hash_key(request)
        ts_utc = _iso_now()
        client = await _get_client()

        params = dict(request.query_params)
        t0 = time.monotonic()

        req = client.build_request(
            method=request.method,
            url=upstream,
            headers=fwd_headers,
            params=params,
            content=body,
        )

        try:
            upstream_resp = await client.send(req, stream=True)
        except httpx.RequestError as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log.warning("upstream request failed: %s", exc)
            _record(
                ts_utc=ts_utc, user_hash=user_hash, model=model,
                endpoint=endpoint, streamed=streamed_req,
                status_code=502, latency_ms=latency_ms,
                input_tokens=None, output_tokens=None,
                cache_read_tokens=None, cache_creation_tokens=None,
            )
            return Response(content=f"upstream error: {exc}".encode(),
                            status_code=502, media_type="text/plain")

        resp_content_type = upstream_resp.headers.get("content-type", "")
        is_sse = "text/event-stream" in resp_content_type.lower()

        # Response headers — copy all except hop-by-hop + length/encoding.
        out_headers = {
            k: v for k, v in upstream_resp.headers.items()
            if k.lower() not in _RESP_STRIP
        }

        if is_sse:
            # Streaming path: yield chunks to the client while teeing the
            # bytes into the SSE usage extractor.
            extractor = _SseUsageExtractor()
            status_code = upstream_resp.status_code

            async def gen() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream_resp.aiter_raw():
                        extractor.feed(chunk)
                        yield chunk
                    extractor.flush()
                finally:
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    try:
                        await upstream_resp.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                    _record(
                        ts_utc=ts_utc, user_hash=user_hash, model=model,
                        endpoint=endpoint, streamed=True,
                        status_code=status_code, latency_ms=latency_ms,
                        input_tokens=extractor.input_tokens,
                        output_tokens=extractor.output_tokens,
                        cache_read_tokens=extractor.cache_read_tokens,
                        cache_creation_tokens=extractor.cache_creation_tokens,
                    )

            return StreamingResponse(
                gen(),
                status_code=status_code,
                headers=out_headers,
                media_type=resp_content_type or "text/event-stream",
            )

        # Non-streaming path: buffer, parse usage, forward.
        try:
            body_bytes = await upstream_resp.aread()
        finally:
            await upstream_resp.aclose()
        latency_ms = int((time.monotonic() - t0) * 1000)
        it, ot, cr, cc = _extract_usage_json(body_bytes)
        _record(
            ts_utc=ts_utc, user_hash=user_hash, model=model,
            endpoint=endpoint, streamed=False,
            status_code=upstream_resp.status_code, latency_ms=latency_ms,
            input_tokens=it, output_tokens=ot,
            cache_read_tokens=cr, cache_creation_tokens=cc,
        )
        return Response(
            content=body_bytes,
            status_code=upstream_resp.status_code,
            headers=out_headers,
            media_type=resp_content_type or None,
        )

    return router
