"""Tests for the /health system dashboard: the psutil sample shape, the cheap
shared cache, the unauthenticated shell page, and the authenticated SSE stream.
"""

import json

import anyio
import pytest
from fastapi.testclient import TestClient

from spliti import health
from spliti.app import health_stream, split_app
from tests.conftest import TEST_PASSWORD

AUTH = ("health", TEST_PASSWORD)


@pytest.fixture
def client():
    with TestClient(split_app) as c:
        yield c


def test_sample_has_expected_shape():
    snap = health.sample()
    for key in ("ts", "uptime_sec", "host", "cpu", "memory", "swap", "disk", "network"):
        assert key in snap
    assert isinstance(snap["cpu"]["percent"], float)
    assert isinstance(snap["cpu"]["per_core"], list)
    assert 0 <= snap["memory"]["percent"] <= 100
    assert snap["disk"]["total"] > 0
    assert snap["network"]["sent_bps"] >= 0 and snap["network"]["recv_bps"] >= 0


def test_sample_is_cached_within_min_interval():
    """Two back-to-back calls reuse the same cached object (cheap for N viewers)."""
    a = health.sample()
    b = health.sample()
    assert a is b


def test_health_page_served_without_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "System Health" in r.text


def test_stream_requires_auth(client):
    assert client.get("/health/stream").status_code == 401


def test_stream_response_is_sse_with_metrics():
    """Drive the route function directly: assert it's an SSE response whose first
    event is a JSON metrics snapshot. (We pull a single event rather than stream
    over HTTP — the generator is infinite and TestClient won't cancel it.)"""
    async def run():
        resp = await health_stream()
        assert resp.media_type == "text/event-stream"
        assert resp.headers["cache-control"] == "no-cache"
        chunk = await resp.body_iterator.__anext__()
        await resp.body_iterator.aclose()
        return chunk

    chunk = anyio.run(run)
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    payload = json.loads(chunk[len("data: "):].strip())
    assert "cpu" in payload and "memory" in payload and "disk" in payload
