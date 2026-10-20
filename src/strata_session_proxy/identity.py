"""Candidates only. Strata remains the authority on token/image prefix compatibility.

Only digests, never messages or responses, leave this module. Unknown request fields
are included rather than guessed to be irrelevant. Mutable external image URLs are
not identities. No generated-answer reconstruction (especially not from SSE).
"""

import hashlib
import json
from dataclasses import dataclass

PATHS = {"/v1/chat/completions", "/chat/completions", "/v1/messages"}
# These affect generation/transport, not interpretation of the incoming history.
GENERATION = {
    "stream",
    "stream_options",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "seed",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "repeat_penalty",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def mutable_media(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {"url", "image_url", "file_id", "file_uri"}:
                if isinstance(v, str) and not v.startswith("data:"):
                    return True
            if mutable_media(v):
                return True
    if isinstance(value, list):
        return any(mutable_media(v) for v in value)
    return False


@dataclass(frozen=True)
class Identity:
    scope: str
    chain: tuple[str, ...]
    anchors: tuple[str, ...]

    @property
    def key(self):
        return self.chain[-1]

    def packed(self):
        return {"scope": self.scope, "chain": self.chain, "anchors": self.anchors}

    @classmethod
    def unpack(cls, value):
        return cls(value["scope"], tuple(value["chain"]), tuple(value["anchors"]))

    def overlap(self, other):
        if other is None or self.scope != other.scope:
            return 0
        # Full previous input prefix, rewind, or a branch after a completed exchange.
        if self.key in other.chain and self.key in self.anchors:
            return len(self.chain)
        if other.key in self.chain and other.key in other.anchors:
            return len(other.chain)
        n = 0
        for i, (a, b) in enumerate(zip(self.chain, other.chain)):
            if a != b:
                break
            if a in self.anchors and i > 1:
                n = i + 1
        return n


def identify(path, body, headers):
    if (
        path not in PATHS
        or headers.get("Content-Encoding")
        or "application/json" not in headers.get("Content-Type", "").lower()
    ):
        return None
    try:

        def pairs(items):
            d = {}
            for k, v in items:
                if k in d:
                    raise ValueError("duplicate key")
                d[k] = v
            return d

        req = json.loads(body, object_pairs_hook=pairs)
        if not isinstance(req, dict) or req.get("strata_checkpoint") is False:
            return None
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages or mutable_media(req):
            return None
        # Separate authenticated tenants without retaining any credential.
        scope = digest(
            [
                path,
                headers.get("Authorization", ""),
                headers.get("x-api-key", ""),
                {k: v for k, v in req.items() if k != "messages" and k not in GENERATION},
            ]
        )
        h = hashlib.sha256(scope.encode())
        chain, anchors = [], []
        seen_user = False
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }:
                return None
            part = canonical(m)
            h.update(len(part).to_bytes(8, "big"))
            h.update(part)
            chain.append(h.hexdigest())
            seen_user |= m["role"] == "user"
            if seen_user:
                anchors.append(chain[-1])
        return Identity(scope, tuple(chain), tuple(anchors)) if seen_user else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
