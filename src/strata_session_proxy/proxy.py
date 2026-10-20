"""HTTP/1.1 reverse forwarding, with an optional single-slot coordination gate."""

import asyncio
import hashlib
import logging

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

from .backend import Backend
from .coordinator import Blocked, Coordinator
from .identity import PATHS, Identity, canonical, identify
from .storage import Store

LOG = logging.getLogger(__name__)
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
# Method-specific slot-read-only policy, audited against the pinned Strata source.
# Do not infer safety from a route name (e.g. /tokenize), or from GET alone.
# See docs/UPSTREAM.md for handlers, aliases and explicit exclusions.
READ_ONLY = {
    "/health",
    "/api/health",
    "/v1/models",
    "/models",
    "/props",
    "/slots",
    "/v1/status",
    "/metrics",
    "/v1/telemetry",
    "/status",
    "/metrics/prometheus",
    "/api/requests",
    "/settings",
    "/config",
    "/mcp",
    "/",
    "/api-monitor",
}
READ_ONLY_FILES = ("/fonts/", "/web/", "/v1/images/files/")
TOKEN_COUNTS = {"/v1/messages/count_tokens", "/v1/v1/messages/count_tokens"}


def slot_read_only(method, raw_path):
    # Strata strips trailing slashes before dispatch but does not URL-decode paths.
    # This normalization classifies only; forward() always retains the original target.
    path = raw_path.rstrip("/") or "/"
    if method == "OPTIONS":
        return True  # Strata's global CORS handler sends 204 without dispatching by path.
    if method in {"GET", "HEAD"}:
        return path in READ_ONLY or path.startswith(READ_ONLY_FILES)
    return method == "POST" and path in TOKEN_COUNTS


CONFIG = web.AppKey("config", object)
CLIENT = web.AppKey("client", aiohttp.ClientSession)
MANAGER = web.AppKey("manager", object)
TASKS = web.AppKey("tasks", set)
RESPONSE_STARTED = (
    web.RequestKey("response_started", bool) if hasattr(web, "RequestKey") else "response_started"
)


def headers(raw, request=False):
    pairs = [(k.decode("utf-8", "surrogateescape"), v.decode("utf-8", "surrogateescape")) for k, v in raw]
    excluded = set(HOP)
    for k, v in pairs:
        if k.lower() == "connection":
            excluded.update(x.strip().lower() for x in v.split(","))
    if request:
        excluded.add("host")  # Route to the configured origin, satisfying Strata Host validation.
    return CIMultiDict((k, v) for k, v in pairs if k.lower() not in excluded)


async def request_body(request, limit):
    """Inspect bounded JSON only; overflow and every other body stream without disk spooling."""
    if not request.can_read_body:
        return None, None
    inspect = (
        request.method == "POST" and request.path in PATHS and not request.headers.get("Content-Encoding")
    )
    if inspect and (request.content_length is None or request.content_length <= limit):
        chunks, n = [], 0
        async for chunk in request.content.iter_chunked(65536):
            chunks.append(chunk)
            n += len(chunk)
            if n > limit:

                async def overflow():
                    for part in chunks:
                        yield part
                    async for part in request.content.iter_chunked(65536):
                        yield part

                return overflow(), None
        body = b"".join(chunks)
        # Query/header extensions can affect prompt interpretation. Treat unfamiliar ones
        # conservatively by incorporating them into the scope (never log them).
        identity_headers = request.headers.copy()
        identity = identify(request.path, body, identity_headers)
        if identity:
            extra = [
                (k.lower(), v)
                for k, v in request.headers.items()
                if k.lower()
                not in {
                    "host",
                    "content-length",
                    "transfer-encoding",
                    "connection",
                    "accept",
                    "accept-encoding",
                    "user-agent",
                    "content-type",
                    "authorization",
                    "x-api-key",
                }
            ]
            salt = hashlib.sha256(canonical([request.query_string, sorted(extra)])).hexdigest()

            # Rehash all boundaries so extensions partition candidates as well as scopes.
            def rehash(h):
                return hashlib.sha256((salt + h).encode()).hexdigest()

            identity = Identity(
                rehash(identity.scope),
                tuple(map(rehash, identity.chain)),
                tuple(map(rehash, identity.anchors)),
                identity.roles,
            )
        return body, identity
    return request.content.iter_chunked(65536), None


async def forward(request, body):
    config = request.app[CONFIG]
    if not request.raw_path.startswith("/"):
        # Origin-form only: never let an absolute/authority-form request target influence
        # the configured upstream authority when constructing the URL.
        return web.json_response(
            {"error": {"message": "origin-form request target required"}}, status=400
        ), False
    url = URL(config.server.backend_url.rstrip("/") + request.raw_path, encoded=True)
    response = None
    try:
        async with request.app[CLIENT].request(
            request.method,
            url,
            data=body,
            headers=headers(request.raw_headers, request=True),
            allow_redirects=False,
        ) as upstream:
            response = web.StreamResponse(
                status=upstream.status, reason=upstream.reason, headers=headers(upstream.raw_headers)
            )
            await response.prepare(request)
            request[RESPONSE_STARTED] = True
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response, upstream.status < 400
    except (aiohttp.ClientError, TimeoutError):
        if response and response.prepared:
            # Never append a proxy error to an already-started SSE/body stream.
            if request.transport:
                request.transport.close()
            raise ConnectionResetError("upstream stream interrupted") from None
        return web.json_response(
            {"error": {"message": "upstream unavailable or timed out"}}, status=502
        ), False


async def coordinated(request, body, identity, disconnected):
    manager = request.app[MANAGER]
    async with manager.lock:
        try:
            return await managed_request(request, body, identity, disconnected, manager)
        except Exception:
            # Includes catalog I/O failures and malformed control acknowledgements. Do not
            # leak upstream bodies, request URLs or exception messages into logs.
            manager.failed = True
            try:
                manager.block("internal_management_failure")
            except Exception:
                pass
            LOG.error("management failed closed; inspect local storage and backend before recovery")
            if request.get(RESPONSE_STARTED) and request.transport:
                request.transport.close()
            return web.json_response(
                {"error": {"message": "session management failed closed; operator recovery required"}},
                status=503,
            )


async def managed_request(request, body, identity, disconnected, manager):
    if disconnected.is_set():
        return web.Response(status=499)
    try:
        before = await manager.before(identity)
    except Blocked:
        return web.json_response(
            {"error": {"message": "session state uncertain; local operator recovery required"}}, status=503
        )
    if disconnected.is_set():
        manager.invalidate()
        return web.Response(status=499)
    task = asyncio.create_task(forward(request, body))

    async def watch():
        await disconnected.wait()
        task.cancel()

    watcher = asyncio.create_task(watch())
    success = False
    result = web.Response(status=499)
    try:
        result, success = await task
    except (asyncio.CancelledError, ConnectionError):
        pass
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        try:
            await manager.after(identity, before, success)
        except Blocked:
            # If response already went out, subsequent mutations are refused. No fabricated
            # error chunks, and no release to another inference while state is ambiguous.
            if not result.prepared:
                result = web.json_response(
                    {"error": {"message": "cannot confirm backend idle; operator recovery required"}},
                    status=503,
                )
    return result


async def handle(request):
    manager = request.app[MANAGER]
    if manager is None:
        body = request.content.iter_chunked(65536) if request.can_read_body else None
        identity = None
    else:
        body, identity = await request_body(request, request.app[CONFIG].server.detection_max_bytes)
    if manager is None or slot_read_only(request.method, request.rel_url.raw_path):
        try:
            return (await forward(request, body))[0]
        except ConnectionError:
            return web.Response(status=502)
    disconnected = asyncio.Event()
    task = asyncio.create_task(coordinated(request, body, identity, disconnected))
    request.app[TASKS].add(task)
    task.add_done_callback(request.app[TASKS].discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Snapshot commands cannot be cancelled halfway. The independent coordinator keeps
        # ownership, cancels ordinary upstream inference, and confirms idle before releasing.
        disconnected.set()
        raise


async def resources(app):
    config = app[CONFIG]
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=config.server.connect_timeout_seconds,
        sock_read=config.server.read_timeout_seconds or None,
    )
    async with aiohttp.ClientSession(
        timeout=timeout,
        auto_decompress=False,
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        skip_auto_headers={"Accept", "User-Agent", "Accept-Encoding", "Content-Type"},
    ) as client:
        app[CLIENT] = client
        store = Store(config.snapshots) if config.snapshots.enabled else None
        app[MANAGER] = Coordinator(Backend(client, config), store, config) if store else None
        try:
            yield
        finally:
            tasks = list(app[TASKS])
            if tasks:
                # Shutdown waits for in-flight management (up to the service manager's stop
                # deadline). A forced kill leaves a durable ambiguity marker.
                await asyncio.gather(*tasks, return_exceptions=True)
            if store:
                store.close()


def create_app(config):
    config.validate()
    app = web.Application(client_max_size=0, handler_args={"auto_decompress": False})
    app[CONFIG] = config
    app[TASKS] = set()
    app.cleanup_ctx.append(resources)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app
