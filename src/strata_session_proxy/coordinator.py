"""Single-slot ownership and snapshot transitions. Never retry ambiguous mutations."""

import asyncio
import hashlib
import json
import logging

from .backend import BackendError, Uncertain
from .storage import StorageError

LOG = logging.getLogger(__name__)


class Blocked(Exception):
    pass


class Coordinator:
    def __init__(self, backend, store, config):
        self.backend, self.store, self.config = backend, store, config
        self.lock = asyncio.Lock()
        self.active = None
        self.observed = None
        self.restored = False
        self.failed = False
        self.store.meta("active", "")  # Never infer active state across proxy restarts.
        self.store.meta("context", "null")

    def invalidate(self):
        self.active = self.observed = None
        self.store.meta("active", "")
        self.store.meta("context", "null")

    def block(self, reason):
        self.failed = True
        self.invalidate()
        self.store.meta("blocked", reason)
        self.store.count("failures")
        LOG.error("session management blocked: %s", reason)
        raise Blocked(reason)

    def compatibility(self, obs):
        # Server boot is intentionally NOT part of compatibility; files may survive restarts.
        return hashlib.sha256(
            json.dumps([self.config.snapshots.compatibility_key, obs.boot[1:]]).encode()
        ).hexdigest()

    async def before(self, identity):
        self.restored = False
        if self.failed or self.store.meta("blocked"):
            raise Blocked("snapshot state requires local operator recovery")
        try:
            obs = await self.backend.idle()
        except BackendError:
            self.invalidate()
            self.store.count("status_unavailable")
            return None  # No snapshot attempt. Ordinary backend processing still works.
        if self.observed and self.observed.stamp != obs.stamp:
            self.invalidate()
            self.store.count("external_changes")
        if identity and self.active and identity.overlap(self.active):
            return obs
        row = None
        try:
            self.store.check()
            if identity:
                row = self.store.candidate(identity, self.compatibility(obs))
        except (OSError, StorageError):
            self.store.count("failures")
        if self.active and self.observed:
            await self.save(self.compatibility(obs), protected=row["name"] if row else None)
        self.invalidate()
        if identity:
            try:
                if row:
                    await self.restore(row)
                else:
                    self.store.count("cache_misses")
            except (OSError, StorageError):
                if self.store.meta("blocked"):
                    self.block("restore_storage_failure")
                self.store.count("failures")
                LOG.warning("snapshot storage unavailable; forwarding without restore")
        return obs

    async def save(self, compatibility, protected=None):
        p = self.config.snapshots
        obs = self.observed
        if not p.auto_save or obs.prompt * 100 < p.save_threshold_percent * obs.context:
            return
        if protected and p.max_snapshots == 1:
            # With one entry there is no safe eviction victim: prioritize the requested restore.
            self.store.count("save_skipped_protected_target")
            return
        name = None
        try:
            name = self.store.begin(self.active, compatibility)
            # Durable ambiguity marker before the network operation; crash recovery is fail closed.
            self.store.meta("blocked", "save_in_progress")
            code, reply = await self.backend.action("save", name)
            if code != 200:
                # Documented SERR is non-mutating. A generic 500 may mean engine death/protocol loss.
                safe_status = code in {400, 401, 403, 404, 415, 501, 503, 507}
                if (
                    not isinstance(reply, dict)
                    or not safe_status
                    and (reply.get("error") or {}).get("kind") not in {"invalid", "storage", "memory", "io"}
                ):
                    self.block("uncertain_save")
                self.store.remove(name)
                self.store.meta("blocked", "")
                self.store.count("failed_saves")
                return
            if (
                reply.get("filename") != name
                or reply.get("id_slot") != 0
                or type(reply.get("n_saved")) is not int
                or reply["n_saved"] < obs.prompt
            ):
                self.block("invalid_save_acknowledgement")
            size, sha = await asyncio.to_thread(self.store.file_info, name)
            if size <= 0 or size != reply.get("n_written"):
                self.block("save_file_not_visible_or_incomplete")
            self.store.commit(name, reply["n_saved"], size, sha, protected=protected)
            self.store.meta("blocked", "")
            self.store.count("saves")
            LOG.info("saved session %s", self.active.key[:12])
        except Uncertain:
            self.block("uncertain_save")
        except (OSError, StorageError):
            self.store.count("failed_saves")
            if self.store.meta("blocked"):
                self.block("save_storage_failure")
            LOG.warning("save skipped: snapshot storage unavailable or reserve reached")

    async def restore(self, row):
        try:
            size, sha = await asyncio.to_thread(self.store.file_info, row["name"])
        except FileNotFoundError:
            # Missing snapshot, not missing mount: check the directory before retiring metadata.
            self.store.check()
            self.store.remove(row["name"])
            self.store.count("failed_restores")
            return
        except (OSError, StorageError):
            self.store.count("failed_restores")
            return
        if size != row["size"] or sha != row["sha256"]:
            self.store.remove(row["name"])
            self.store.count("corrupt_snapshots")
            return
        self.store.meta("blocked", "restore_in_progress")
        try:
            code, reply = await self.backend.action("restore", row["name"])
        except Uncertain:
            self.block("uncertain_restore")
        if code == 200:
            if (
                reply.get("filename") != row["name"]
                or reply.get("id_slot") != 0
                or reply.get("n_restored") != row["tokens"]
                or reply.get("n_read") != row["size"]
            ):
                self.block("invalid_restore_acknowledgement")
            self.store.touch(row["name"])
            self.store.count("restores")
            self.restored = True
            LOG.info("restored snapshot %s", row["name"])
        elif code in (400, 404) or code == 503 and (reply.get("error") or {}).get("kind") == "memory":
            # Strata validates before writing to the device; these refusals leave the slot unchanged.
            if code in (400, 404):
                self.store.remove(row["name"])
            self.store.count("failed_restores")
        else:
            self.block("uncertain_restore")
        self.store.meta("blocked", "")

    async def after(self, identity, before, success):
        if before is None:
            # Unsupported/unavailable status: we did not issue a snapshot operation, and
            # the next request must establish idle independently before any such operation.
            self.invalidate()
            return
        try:
            obs = await self.backend.idle()
        except BackendError:
            # Closed/cancelled HTTP does not prove the engine stopped. Retain the gate until
            # it is observed idle; if it cannot be observed, require operator intervention.
            self.block("cannot_confirm_idle_after_request")
        if (
            not success
            or not identity
            or not before
            or obs.boot != before.boot
            or obs.requests != before.requests + 1
            or not obs.prompt
        ):
            self.invalidate()
            return
        self.active, self.observed = identity, obs
        self.store.meta("active", identity.key[:16])
        self.store.meta(
            "context",
            json.dumps(
                {
                    "prompt_tokens_lower_bound": obs.prompt,
                    "limit": obs.context,
                    "percent_lower_bound": 100 * obs.prompt / obs.context,
                }
            ),
        )
        if self.restored:
            if obs.cached:
                self.store.count("cache_hits")
                # Actual reported reused tokens, estimated attribution: in-RAM reuse might have
                # happened without us. Never describe this as measured incremental energy savings.
                self.store.count("estimated_prompt_tokens_spared", min(obs.cached, obs.prompt))
            else:
                self.store.count("restore_without_reported_hit")
