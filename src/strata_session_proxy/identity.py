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
    # Request-only boundary information, kept in memory. Persisted identities are
    # candidates, never authoritative active histories, so the catalog schema is unchanged.
    roles: tuple[str, ...] = ()

    def continues(self, active):
        """A directional input-history extension, NOT proof of the live KV/output tokens.

        Replays, rewinds, siblings and assistant-prefill/unclear response boundaries
        take the switch path. A normal next turn preserves the ENTIRE previous input,
        appends an assistant response, and ends at a new user/tool request boundary.
        The assistant's correspondence to the generated response cannot be proved
        from incoming histories alone; Strata still checks the actual token prefix.
        """
        if active is None or self.scope != active.scope:
            return False
        n = len(active.chain)
        return bool(
            n
            and len(self.chain) > n
            and self.chain[:n] == active.chain
            and active.key in active.anchors
            and len(active.roles) == n
            and len(self.roles) == len(self.chain)
            and active.roles[-1] in {"user", "tool"}
            and self.roles[n] == "assistant"
            and self.roles[-1] in {"user", "tool"}
            # Some templates collect system/developer messages at the prompt's start;
            # appending one is not evidence that the rendered old prefix is preserved.
            and all(role in {"assistant", "user", "tool"} for role in self.roles[n:])
        )

    @property
    def key(self):
        return self.chain[-1]

    def packed(self):
        return {"scope": self.scope, "chain": self.chain, "anchors": self.anchors}

    @classmethod
    def unpack(cls, value):
        return cls(value["scope"], tuple(value["chain"]), tuple(value["anchors"]))

    def reusable_prefix(self, saved):
        """Conservative disk candidate score: the WHOLE saved input must be a prefix.

        A disk file keeps one selected (deepest, or pinned) checkpoint, not every boundary.
        Partial overlap, rewinds and moved suffixes do not prove that checkpoint
        is compatible. A complete prior input can nominate its end-of-input/turn
        checkpoint; Strata still decides exact token/image compatibility. No
        message-count, text-length or token-overlap threshold is guessed here.
        """
        if saved is None or self.scope != saved.scope or saved.key not in saved.anchors:
            return 0
        n = len(saved.chain)
        return n if len(self.chain) >= n and self.chain[:n] == saved.chain else 0

    def overlap(self, other):
        """Relationship score only: shared ordered user-bearing prefix.

        Not sufficient for disk selection (use reusable_prefix) or continuation.
        """
        if other is None or self.scope != other.scope:
            return 0
        # Full input prefix, rewind or shared user-bearing prefix of sibling branches.
        if self.key in other.chain and self.key in self.anchors:
            return len(self.chain)
        if other.key in self.chain and other.key in other.anchors:
            return len(other.chain)
        n = 0
        for i, (a, b) in enumerate(zip(self.chain, other.chain)):
            if a != b:
                break
            if a in self.anchors:
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
        chain, anchors, roles = [], [], []
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
            roles.append(m["role"])
            part = canonical(m)
            h.update(len(part).to_bytes(8, "big"))
            h.update(part)
            chain.append(h.hexdigest())
            seen_user |= m["role"] == "user"
            if seen_user:
                anchors.append(chain[-1])
        return Identity(scope, tuple(chain), tuple(anchors), tuple(roles)) if seen_user else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
