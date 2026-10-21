"""All sockets bind loopback on ephemeral ports. No external/live endpoint is used."""

import asyncio
import gzip
import json
import os
import socket
from contextlib import AsyncExitStack

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from strata_session_proxy.config import Config
from strata_session_proxy.proxy import MANAGER, create_app


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    """Reject accidental external/live-server connections, even if a test is edited incorrectly."""
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def check(address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("tests may only connect to loopback mock servers")

    def connect(sock, address):
        check(address)
        return original(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)

    def resolve(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            raise AssertionError("tests may not resolve external hosts")
        return original_resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)


class MockStrata:
    def __init__(self, directory):
        self.directory = directory
        self.started = 100
        self.engine = "synthetic-engine-v1"
        self.model = "synthetic-model"
        self.requests = 0
        self.busy = 0
        self.prompt = 0
        self.context = 1000
        self.next_tokens = 400
        self.cached = 0
        self.cache_override = None
        self.timings_override = "auto"
        self.include_usage = False
        self.held = None
        self.loaded = True
        self.events = []
        self.snapshot_actions = []
        self.control_calls = []
        self.received = []
        self.delay = 0
        self.inference_delay = 0
        self.stream_gate = None
        self.stream_started = asyncio.Event()
        self.save_error = None
        self.restore_error = None
        self.max_busy = 0
        self.fifo = asyncio.Lock()

    async def handle(self, r):
        if r.path == "/v1/status":
            self.control_calls.append(r.path)
            return web.json_response(
                {
                    "service": "strata",
                    "loaded": self.loaded,
                    "started": self.started,
                    "engine": self.engine,
                    "model": self.model,
                    "concurrency": {"serving": 1},
                    "activity": {"requests": self.requests, "in_flight": self.busy},
                    "last_timings": (
                        {"cache_n": self.cached, "prompt_n": self.prompt - self.cached}
                        if self.timings_override == "auto"
                        else self.timings_override
                    ),
                }
            )
        if r.path == "/slots":
            self.control_calls.append(r.path)
            return web.json_response(
                [
                    {
                        "id": 0,
                        "n_ctx": self.context,
                        "is_processing": bool(self.busy),
                        "n_prompt_tokens": self.prompt,
                    }
                ]
                if self.loaded
                else []
            )
        if r.path == "/slots/0" and r.method == "POST":
            body = await r.json()
            action, name = r.query["action"], body["filename"]
            async with self.fifo:
                assert not self.busy
                self.events.append(action)
                self.snapshot_actions.append((action, name))
                self.prompt = 0  # Actual Strata slot_action clears status prompt_tokens.
                await asyncio.sleep(self.delay)
                err = self.save_error if action == "save" else self.restore_error
                if err:
                    return web.json_response(err[1], status=err[0])
                path = self.directory / name
                if action == "save":
                    if self.held is None:
                        return web.json_response({"error": {"kind": "invalid"}}, status=400)
                    data = json.dumps(
                        {"model": self.model, "engine": self.engine, "held": self.held}
                    ).encode()
                    path.write_bytes(data)
                    os.chmod(path, 0o600)
                    return web.json_response(
                        {
                            "id_slot": 0,
                            "filename": name,
                            "n_saved": self.held["tokens"],
                            "n_written": len(data),
                        }
                    )
                try:
                    saved = json.loads(path.read_bytes())
                    assert saved["model"] == self.model and saved["engine"] == self.engine
                except (OSError, ValueError, AssertionError):
                    return web.json_response({"error": {"kind": "invalid"}}, status=400)
                self.held = saved["held"]
                return web.json_response(
                    {
                        "id_slot": 0,
                        "filename": name,
                        "n_restored": self.held["tokens"],
                        "n_read": path.stat().st_size,
                    }
                )
        body = await r.read()
        self.received.append((r.method, r.raw_path, dict(r.headers), body))
        if r.method == "OPTIONS" and "status" not in r.query:
            # Explicit status= remains available to the arbitrary-HTTP forwarding tests.
            return web.Response(status=204)
        if r.method == "POST" and r.rel_url.raw_path.rstrip("/") in {
            "/v1/messages/count_tokens",
            "/v1/v1/messages/count_tokens",
        }:
            # Synthetic tokenization only: no engine/FIFO/live-slot/counter mutation.
            return web.json_response({"input_tokens": len(json.loads(body).get("messages", [])) * 10})
        if r.path in {"/v1/chat/completions", "/v1/messages"} and r.method == "POST":
            data = json.loads(body)
            async with self.fifo:
                self.busy += 1
                self.max_busy = max(self.max_busy, self.busy)
                self.requests += 1
                self.prompt = self.next_tokens
                self.events.append("inference")
                previous = self.held
                self.cached = (
                    min(self.prompt, previous["tokens"] - 5)
                    if previous and data["messages"][: len(previous["messages"])] == previous["messages"]
                    else 0
                )
                if self.cache_override is not None:
                    self.cached = self.cache_override
                try:
                    await asyncio.sleep(self.inference_delay)
                    self.held = {"messages": data["messages"], "tokens": self.prompt + 5}
                    if data.get("stream"):
                        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                        await response.prepare(r)
                        await response.write(b'data: {"delta":{"reasoning_content":"synthetic"}}\n\n')
                        self.stream_started.set()
                        if data.get("synthetic_abort"):
                            r.transport.close()
                            return response
                        if self.stream_gate:
                            await self.stream_gate.wait()
                        await response.write(b'data: {"delta":{"tool_calls":[]}}\n\ndata: [DONE]\n\n')
                        await response.write_eof()
                        return response
                    if self.include_usage:
                        return web.json_response(
                            {
                                "choices": [{"message": {"role": "assistant", "content": "synthetic"}}],
                                "usage": {
                                    "prompt_tokens": self.prompt,
                                    "completion_tokens": 5,
                                    "total_tokens": self.prompt + 5,
                                    "prompt_tokens_details": {"cached_tokens": self.cached},
                                },
                            }
                        )
                    return web.Response(
                        body=b'{"choices":[{"message":{"role":"assistant","content":"synthetic"}}]}',
                        content_type="application/json",
                    )
                finally:
                    self.busy -= 1
        if r.path == "/synthetic-compressed":
            return web.Response(
                body=gzip.compress(b"synthetic compressed response", mtime=0),
                headers={"Content-Encoding": "gzip", "Content-Type": "application/octet-stream"},
            )
        status = int(r.query.get("status", "200"))
        return web.Response(
            body=body if body else b"synthetic-response",
            status=status,
            headers=[("X-Synthetic", "yes"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")],
        )


async def start(app, stack):
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True, auto_decompress=False)
    await runner.setup()
    stack.push_async_callback(runner.cleanup)
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


@pytest_asyncio.fixture
async def harness(tmp_path):
    async with AsyncExitStack() as stack:
        directory = tmp_path / "snapshots"
        directory.mkdir(mode=0o700)
        mock = MockStrata(directory)
        app = web.Application(client_max_size=0, handler_args={"auto_decompress": False})
        app.router.add_route("*", "/{tail:.*}", mock.handle)
        backend_url = await start(app, stack)
        client = await stack.enter_async_context(aiohttp.ClientSession(auto_decompress=False))
        proxies = []

        async def make(enabled=True, **settings):
            config = Config()
            config.server.backend_url = backend_url
            config.snapshots.enabled = enabled
            config.snapshots.exclusive_backend = True
            config.snapshots.compatibility_key = "synthetic-deployment-v1"
            config.snapshots.directory = str(directory)
            config.snapshots.state_directory = str(tmp_path / f"state-{len(proxies)}")
            config.snapshots.min_free_disk_gib = 0
            config.snapshots.poll_interval_seconds = 0.01
            config.snapshots.idle_timeout_seconds = 0.2
            for k, v in settings.items():
                setattr(config.snapshots, k, v)
            proxy = create_app(config)
            url = await start(proxy, stack)
            proxies.append(proxy)
            return url, proxy[MANAGER], config

        yield mock, client, make, backend_url


def conversation(name="alpha", continuation=False, **extra):
    messages = [{"role": "system", "content": "synthetic shared system"}, {"role": "user", "content": name}]
    if continuation:
        messages += [{"role": "assistant", "content": "synthetic"}, {"role": "user", "content": "continue"}]
    return {"messages": messages, **extra}


async def infer(client, url, name="alpha", continuation=False, **extra):
    async with client.post(url + "/v1/chat/completions", json=conversation(name, continuation, **extra)) as r:
        data = await r.read()
        assert r.status == 200, (r.status, data)
        return data


async def settled(manager):
    # HTTP EOF can arrive just before the coordinator's postflight observation.
    async with manager.lock:
        pass
