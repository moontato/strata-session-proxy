"""Audited slot-read-only methods bypass management; unknown methods/routes do not."""

import asyncio
import json

import pytest
from conftest import conversation, infer, settled
from yarl import URL

from strata_session_proxy.proxy import slot_read_only

READ_ONLY_CASES = [
    ("GET", "/health"),
    ("GET", "/api/health"),
    ("GET", "/models"),
    ("GET", "/v1/models"),
    ("GET", "/props"),
    ("GET", "/slots"),
    ("GET", "/v1/status"),
    ("GET", "/metrics?requests=all"),
    ("GET", "/v1/telemetry"),
    ("GET", "/status"),
    ("GET", "/metrics/prometheus"),
    ("GET", "/api/requests?id=synthetic"),
    ("GET", "/settings"),
    ("GET", "/config"),
    ("GET", "/mcp"),
    ("GET", "/"),
    ("GET", "/api-monitor"),
    ("GET", "/fonts/synthetic.woff2"),
    ("GET", "/web/synthetic.js"),
    ("GET", "/v1/images/files/synthetic.png"),
    ("GET", "/metrics/prometheus///?format=synthetic"),
    ("HEAD", "/config"),
    ("POST", "/v1/messages/count_tokens"),
    ("POST", "/v1/v1/messages/count_tokens/?beta=true"),
    ("OPTIONS", "/v1/chat/completions"),
    ("OPTIONS", "/unknown-synthetic-path"),
]


@pytest.mark.parametrize("method,path", READ_ONLY_CASES)
async def test_read_only_requests_leave_active_identity_and_catalog_untouched(harness, method, path):
    mock, client, make, backend = harness
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    active, observed = manager.active, manager.observed
    changes = manager.store.db.total_changes
    events = list(mock.events)
    body = json.dumps(conversation(), indent=3).encode() if method == "POST" else None
    headers = {"Content-Type": "application/json", "Authorization": "Bearer synthetic-test-only"}
    async with client.request(method, backend + path, data=body, headers=headers) as direct:
        expected_status, expected = direct.status, await direct.read()
    async with client.request(method, url + path, data=body, headers=headers) as response:
        assert response.status == expected_status
        assert await response.read() == expected
    assert manager.active is active and manager.observed is observed
    assert manager.store.db.total_changes == changes
    assert mock.events == events
    if body is not None:
        assert mock.received[-1][3] == body
        assert mock.received[-1][2]["Authorization"] == headers["Authorization"]
    # No invalidation means the following genuine continuation still avoids a save.
    await infer(client, url, continuation=True)
    await settled(manager)
    assert "save" not in mock.events and "restore" not in mock.events


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/future-synthetic-endpoint"),
        ("POST", "/future-synthetic-endpoint"),
        ("POST", "/settings"),
        ("POST", "/config"),
        ("POST", "/load"),
        ("POST", "/unload"),
        ("POST", "/v1/load"),
        ("POST", "/v1/unload"),
        ("POST", "/v1/vram"),
        ("POST", "/v1/images/generations"),
        ("POST", "/v1/responses"),
        ("POST", "/tokenize"),
        ("POST", "/detokenize"),
        ("DELETE", "/config"),
        ("POST", "/metrics"),
        ("POST", "/v1/messages/count_tokens/unknown"),
        ("POST", "/v1/messages/count_tokens%2F"),
    ],
)
async def test_unknown_or_mutating_requests_are_not_bypassed(harness, method, path):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    body = b'{"synthetic":"unchanged"}'
    async with client.request(method, URL(url + path, encoded=True), data=body) as response:
        assert response.status == 200
        assert await response.read() == body
    await settled(manager)
    assert mock.events == ["inference", "save"]
    assert manager.active is None
    assert mock.received[-1][0:2] == (method, path)
    assert mock.received[-1][3] == body


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses", "/slots/0"])
def test_inference_and_slot_controls_stay_gated(path):
    assert not slot_read_only("POST", path)


async def test_token_count_bypasses_in_flight_stream_without_releasing_inference_gate(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.stream_gate = asyncio.Event()
    async with client.post(url + "/v1/chat/completions", json=conversation(stream=True)) as stream:
        first = await stream.content.readuntil(b"\n\n")
        assert b"reasoning_content" in first
        changes = manager.store.db.total_changes
        async with asyncio.timeout(0.5):
            async with client.post(url + "/v1/messages/count_tokens", json=conversation()) as count:
                assert count.status == 200
                assert await count.json() == {"input_tokens": 20}
        assert manager.lock.locked() and mock.busy == 1
        assert manager.store.db.total_changes == changes
        mock.stream_gate.set()
        assert await stream.read() == b'data: {"delta":{"tool_calls":[]}}\n\ndata: [DONE]\n\n'
    await settled(manager)
    assert mock.events == ["inference"]


async def test_read_only_token_count_bypasses_recovery_latch_without_clearing_it(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    manager.store.meta("blocked", "synthetic-test-block")
    changes = manager.store.db.total_changes
    async with client.post(url + "/v1/messages/count_tokens", json=conversation()) as response:
        assert response.status == 200
        await response.read()
    assert manager.store.meta("blocked") == "synthetic-test-block"
    assert manager.store.db.total_changes == changes
    assert not mock.events
