import asyncio
import gzip
import json

import pytest
from conftest import conversation, infer, settled
from yarl import URL


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_arbitrary_forwarding(harness, method):
    mock, client, make, backend = harness
    url, _, _ = await make(False)
    path = "/arbitrary/%2Fpart?x=a%2Bb&x=two&status=418"
    body = b"\x00synthetic\xff"
    kwargs = {"data": body, "headers": {"Authorization": "Bearer synthetic-test-only", "X-Test": "yes"}}
    async with client.request(method, URL(backend + path, encoded=True), **kwargs) as direct:
        expected = await direct.read()
    async with client.request(method, URL(url + path, encoded=True), **kwargs) as r:
        assert r.status == 418
        assert await r.read() == expected
        assert r.headers.getall("Set-Cookie") == ["a=1", "b=2"]
    assert mock.received[-1][1] == path
    assert mock.received[-1][2]["Authorization"] == "Bearer synthetic-test-only"
    assert mock.received[-1][3] == body


async def test_hop_by_hop_and_cookie_isolation(harness):
    mock, client, make, _ = harness
    url, _, _ = await make(False)
    async with client.get(url + "/first", headers={"Connection": "X-Remove", "X-Remove": "secret-hop"}) as r:
        await r.read()
    async with client.get(url + "/second") as r:
        await r.read()
    assert "X-Remove" not in mock.received[0][2]
    assert "Cookie" not in mock.received[1][2]


async def test_multimodal_tools_reasoning_unchanged(harness):
    mock, client, make, backend = harness
    url, _, _ = await make(False)
    data = conversation()
    data["messages"] += [
        {
            "role": "assistant",
            "reasoning_content": "synthetic reasoning",
            "content": None,
            "tool_calls": [
                {
                    "id": "synthetic-call",
                    "type": "function",
                    "function": {"name": "synthetic", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "synthetic-call", "content": "synthetic result"},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}],
        },
    ]
    body = json.dumps(data, indent=3).encode()
    async with client.post(backend + "/v1/chat/completions", data=body) as direct:
        expected = await direct.read()
    async with client.post(url + "/v1/chat/completions", data=body) as r:
        assert await r.read() == expected
    assert mock.received[-1][3] == body


@pytest.mark.parametrize("enabled", [False, True])
async def test_sse_is_incremental_and_byte_exact(harness, enabled):
    mock, client, make, _ = harness
    url, manager, _ = await make(enabled)
    mock.stream_gate = asyncio.Event()
    async with client.post(url + "/v1/chat/completions", json=conversation(stream=True)) as r:
        first = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 1)
        assert first == b'data: {"delta":{"reasoning_content":"synthetic"}}\n\n'
        assert not mock.stream_gate.is_set()
        mock.stream_gate.set()
        assert await r.read() == b'data: {"delta":{"tool_calls":[]}}\n\ndata: [DONE]\n\n'
    if manager:
        await settled(manager)


async def test_large_chunked_body_and_compression(harness):
    mock, client, make, _ = harness
    url, _, config = await make(False)
    raw = bytes(range(256)) * 12000

    async def chunks():
        for i in range(0, len(raw), 7013):
            yield raw[i : i + 7013]

    async with client.post(url + "/unknown", data=chunks()) as r:
        assert await r.read() == raw
    zipped = gzip.compress(raw)
    async with client.post(url + "/unknown", data=zipped, headers={"Content-Encoding": "gzip"}) as r:
        assert await r.read() == zipped
    assert mock.received[-1][3] == zipped


async def test_disabled_has_no_control_calls_or_storage(harness):
    mock, client, make, _ = harness
    url, manager, config = await make(False)
    await infer(client, url)
    await infer(client, url, "beta")
    assert manager is None
    assert mock.events == ["inference", "inference"]
    assert mock.control_calls == []
    from pathlib import Path

    assert not Path(config.snapshots.state_directory).exists()


async def test_head(harness):
    _, client, make, _ = harness
    url, _, _ = await make(False)
    async with client.head(url + "/unknown") as r:
        assert await r.read() == b""
        assert int(r.headers["Content-Length"]) == len(b"synthetic-response")
