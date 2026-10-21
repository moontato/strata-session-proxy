"""Only documented Strata HTTP APIs. Control credentials never replace client credentials."""

import asyncio
import os
import time
from dataclasses import dataclass

import aiohttp


class BackendError(Exception):
    pass


class Uncertain(BackendError):
    pass


@dataclass(frozen=True)
class Observation:
    boot: tuple
    requests: int
    prompt: int
    context: int
    idle: bool
    cached: int | None

    @property
    def stamp(self):
        return self.boot, self.requests, self.prompt, self.context


class Backend:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.url = config.server.backend_url.rstrip("/")

    def headers(self):
        token = os.environ.get(self.config.snapshots.admin_token_env, "")
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self, path, body=None):
        timeout = (
            self.config.snapshots.operation_timeout_seconds
            if body is not None
            else self.config.server.connect_timeout_seconds
        )

        # Explicit JSON Content-Type for internal Strata control requests.
        headers = self.headers()
        if body is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with self.client.request(
                "POST" if body is not None else "GET",
                self.url + path,
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as r:
                # Bounded control response; never include upstream bodies in logs/errors.
                raw = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 1024 * 1024:
                        raise ValueError("oversized control reply")
                import json

                return r.status, json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, ValueError) as e:
            raise Uncertain("control operation did not return a valid response") from e

    async def observe(self):
        code, status = await self.json("/v1/status")
        code2, slots = await self.json("/slots")
        try:
            if code != 200 or code2 != 200 or status["service"] != "strata" or not status["loaded"]:
                raise ValueError()
            if status["concurrency"]["serving"] != 1 or len(slots) != 1 or slots[0]["id"] != 0:
                raise ValueError()
            slot = slots[0]
            requests, prompt, ctx = status["activity"]["requests"], slot["n_prompt_tokens"], slot["n_ctx"]
            if any(type(v) is not int or v < 0 for v in (requests, prompt, ctx)) or not ctx or prompt > ctx:
                raise ValueError()
            boot = (status["started"], status["engine"], status["model"], ctx)
            # Missing/malformed telemetry is UNKNOWN, never a confirmed miss. If
            # present, freshly read + reused must describe this observed prompt.
            timings = status.get("last_timings")
            cached = timings.get("cache_n") if isinstance(timings, dict) else None
            read = timings.get("prompt_n") if isinstance(timings, dict) else None
            if (
                type(cached) is not int
                or not 0 <= cached <= prompt
                or type(read) is not int
                or read < 0
                or read + cached != prompt
            ):
                cached = None
            return Observation(
                boot,
                requests,
                prompt,
                ctx,
                not slot["is_processing"] and status["activity"]["in_flight"] == 0,
                cached,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            raise BackendError("single-slot status unavailable or unsupported") from None

    async def idle(self):
        until = time.monotonic() + self.config.snapshots.idle_timeout_seconds
        while True:
            obs = await self.observe()
            if obs.idle:
                return obs
            if time.monotonic() >= until:
                raise Uncertain("backend did not become idle")
            await asyncio.sleep(self.config.snapshots.poll_interval_seconds)

    async def action(self, action, name):
        return await self.json(f"/slots/0?action={action}", {"filename": name})
