import asyncio
import json
import socket

import aiohttp
import pytest
from conftest import conversation, infer, settled, start

from strata_session_proxy.config import Config
from strata_session_proxy.proxy import create_app, headers


@pytest.mark.parametrize("limit", [1, 2])
async def test_incoming_target_never_evicted_by_outgoing_save(harness, limit):
    mock, client, make, _ = harness
    url, manager, _ = await make(max_snapshots=limit)
    await infer(client, url)
    await infer(client, url, "beta")
    await infer(client, url, continuation=True)
    await settled(manager)
    assert mock.events.count("restore") == 1
    assert len(manager.store.rows()) <= limit


async def test_proxy_restart_retains_candidates_not_active_state(harness):
    mock, client, make, _ = harness
    url, manager, config = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    manager.store.close()  # Simulate completed resource teardown; old listener is never used again.
    url2, manager2, _ = await make(state_directory=config.snapshots.state_directory)
    assert manager2.active is None
    await infer(client, url2, continuation=True)
    await settled(manager2)
    assert mock.events.count("restore") == 1
    assert mock.events.count("save") == 1  # startup must not invent active beta ownership


async def test_compatibility_key_change_partitions_catalog(harness):
    mock, client, make, _ = harness
    url, manager, config = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    manager.store.close()
    url2, manager2, _ = await make(
        state_directory=config.snapshots.state_directory, compatibility_key="synthetic-new-build"
    )
    await infer(client, url2, continuation=True)
    await settled(manager2)
    assert "restore" not in mock.events


async def test_save_timeout_leaves_durable_gate(harness):
    mock, client, make, _ = harness
    url, manager, config = await make(operation_timeout_seconds=0.02)
    await infer(client, url)
    mock.delay = 0.1
    async with client.post(url + "/v1/chat/completions", json=conversation("beta")) as r:
        assert r.status == 503
        assert b"operator" in await r.read()
    assert manager.store.meta("blocked") == "uncertain_save"
    assert mock.events.count("inference") == 1
    await asyncio.sleep(0.12)
    manager.store.close()
    url2, manager2, _ = await make(state_directory=config.snapshots.state_directory)
    async with client.post(url2 + "/v1/chat/completions", json=conversation("gamma")) as r:
        assert r.status == 503
    # Read-only requests are not blocked by the circuit breaker.
    async with client.get(url2 + "/v1/status") as r:
        assert r.status == 200


async def test_published_save_failure_does_not_replace_good_copy(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    old = manager.store.rows()[0]["name"]
    mock.save_error = (500, {"error": {"kind": "io", "published": True}})
    await infer(client, url, "gamma")
    await settled(manager)
    assert (mock.directory / old).exists()
    assert len(manager.store.rows()) == 1
    assert not manager.store.meta("blocked")


async def test_missing_status_falls_back_without_snapshot_calls(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.loaded = False
    await infer(client, url)
    await infer(client, url, "beta")
    await settled(manager)
    assert mock.events == ["inference", "inference"]
    assert not manager.store.meta("blocked")


async def test_backend_unavailable_502_without_credentials_or_urls(tmp_path):
    from contextlib import AsyncExitStack

    # Keep an ephemeral TCP port bound but not listening: it cannot accidentally target a service.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        config = Config()
        config.server.backend_url = f"http://127.0.0.1:{port}"
        async with AsyncExitStack() as stack:
            url = await start(create_app(config), stack)
            client = await stack.enter_async_context(aiohttp.ClientSession())
            async with client.post(url + "/synthetic-sensitive?not-for-log=yes", data=b"synthetic") as r:
                assert r.status == 502
                body = await r.text()
                assert "127.0.0.1" not in body and "not-for-log" not in body


async def test_two_client_dialects_no_special_headers(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    for path, name in [
        ("/v1/chat/completions", "alpha"),
        ("/v1/messages", "beta"),
        ("/v1/chat/completions", "alpha"),
    ]:
        async with client.post(url + path, json=conversation(name)) as r:
            assert r.status == 200
            await r.read()
    await settled(manager)
    assert mock.events.count("restore") == 1
    assert all("session" not in k.lower() for _, _, hdrs, _ in mock.received for k in hdrs)


async def test_inspection_overflow_still_streams_original_body(harness):
    mock, client, make, _ = harness
    url, manager, config = await make()
    config.server.detection_max_bytes = 64
    body = json.dumps(conversation("synthetic-" * 1000)).encode()

    async def chunks():
        for i in range(0, len(body), 13):
            yield body[i : i + 13]

    async with client.post(
        url + "/v1/chat/completions", data=chunks(), headers={"Content-Type": "application/json"}
    ) as r:
        assert r.status == 200
        await r.read()
    await settled(manager)
    assert mock.received[-1][3] == body
    assert manager.active is None


async def test_control_auth_not_injected_into_client_requests(harness, monkeypatch):
    mock, client, make, _ = harness
    monkeypatch.setenv("STRATA_PROXY_BACKEND_TOKEN", "synthetic-admin-token-not-a-secret")
    url, manager, _ = await make()
    await infer(client, url)
    await settled(manager)
    assert manager.backend.headers() == {"Authorization": "Bearer synthetic-admin-token-not-a-secret"}
    assert "Authorization" not in mock.received[-1][2]


async def test_read_only_does_not_wait_for_inference(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    mock.stream_gate = asyncio.Event()
    response = await client.post(url + "/v1/chat/completions", json=conversation(stream=True))
    await response.content.readuntil(b"\n\n")
    async with asyncio.timeout(0.5):
        async with client.get(url + "/v1/status") as r:
            assert (await r.json())["activity"]["in_flight"] == 1
    mock.stream_gate.set()
    await response.read()
    response.close()
    await settled(manager)


async def test_truncated_backend_stream_closes_without_injected_error(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    async with client.post(
        url + "/v1/chat/completions", json=conversation(stream=True, synthetic_abort=True)
    ) as r:
        assert r.status == 200
        with pytest.raises(aiohttp.ClientPayloadError):
            await r.read()
    await settled(manager)
    assert manager.active is None
    await infer(client, url, "beta")
    await settled(manager)
    assert "save" not in mock.events


async def test_malformed_save_acknowledgement_fails_closed(harness):
    mock, client, make, _ = harness
    url, manager, _ = await make()
    await infer(client, url)
    mock.save_error = (200, {"synthetic_wrong_schema": True})
    async with client.post(url + "/v1/chat/completions", json=conversation("beta")) as r:
        assert r.status == 503
    assert manager.store.meta("blocked") == "invalid_save_acknowledgement"


async def test_compressed_response_preserved_byte_for_byte(harness):
    _, client, make, backend = harness
    url, _, _ = await make(False)
    async with client.get(backend + "/synthetic-compressed") as direct:
        expected = await direct.read()
    async with client.get(url + "/synthetic-compressed") as response:
        assert response.headers["Content-Encoding"] == "gzip"
        assert await response.read() == expected


def test_response_hop_by_hop_removal():
    filtered = headers(
        [
            (b"Connection", b"X-Remove, keep-alive"),
            (b"X-Remove", b"no"),
            (b"Transfer-Encoding", b"chunked"),
            (b"Set-Cookie", b"a=1"),
            (b"Set-Cookie", b"b=2"),
        ]
    )
    assert set(filtered) == {"Set-Cookie"}
    assert filtered.getall("Set-Cookie") == ["a=1", "b=2"]


async def test_absolute_target_cannot_override_backend_origin(harness):
    mock, _, make, _ = harness
    url, _, _ = await make(False)
    from urllib.parse import urlsplit

    reader, writer = await asyncio.open_connection("127.0.0.1", urlsplit(url).port)
    writer.write(
        b"GET http://example.invalid/synthetic HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    )
    await writer.drain()
    reply = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert reply.startswith(b"HTTP/1.1 400 ")
    assert mock.received == []


async def test_raw_http_chunked_payload_and_encoded_query(harness):
    mock, _, make, _ = harness
    url, _, _ = await make(False)
    from urllib.parse import urlsplit

    port = urlsplit(url).port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"POST /future/%2f?q=a%2Bb&q=c HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n"
    )
    await writer.drain()
    reply = await reader.read()
    writer.close()
    await writer.wait_closed()
    assert reply.startswith(b"HTTP/1.1 200 ")
    assert reply.endswith(b"onetwo")
    assert mock.received[-1][1] == "/future/%2f?q=a%2Bb&q=c"
    assert mock.received[-1][3] == b"onetwo"
    assert not {"Accept", "User-Agent", "Accept-Encoding", "Content-Type"} & mock.received[-1][2].keys()
