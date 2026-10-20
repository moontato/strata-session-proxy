"""Private SQLite catalog and owned-file-only retention. One process per state directory."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from .identity import Identity

OWNED = re.compile(r"ssp-[0-9a-f]{32}\.bin\Z")


class StorageError(Exception):
    pass


def private_directory(path, create=False):
    p = Path(path)
    if create:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = p.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077:
        raise StorageError("storage directory must be a real, owner-only directory owned by this user")
    return p


class Store:
    def __init__(self, config):
        self.config = config
        self.directory = private_directory(config.directory)
        self.state = private_directory(config.state_directory, create=True)
        self.check_mount()
        st = self.directory.stat()
        self.identity = (st.st_dev, st.st_ino)
        self.lock_file = open(self.state / "owner.lock", "a+b")
        os.chmod(self.state / "owner.lock", 0o600)
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise StorageError("another proxy owns this state directory") from None
        self.snapshot_lock = open(self.directory / ".ssp-owner.lock", "a+b")
        os.chmod(self.directory / ".ssp-owner.lock", 0o600)
        try:
            fcntl.flock(self.snapshot_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.snapshot_lock.close()
            self.lock_file.close()
            raise StorageError("another proxy owns this snapshot directory") from None
        dbpath = self.state / "catalog.sqlite3"
        if dbpath.is_symlink():
            raise StorageError("refusing symlink database")
        self.db = sqlite3.connect(dbpath)
        os.chmod(dbpath, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS snapshots (
                name TEXT PRIMARY KEY, identity TEXT NOT NULL, compatibility TEXT NOT NULL,
                tokens INTEGER NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                used REAL NOT NULL, complete INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value REAL NOT NULL);
        """)
        self.reconcile()

    def close(self):
        self.db.close()
        self.snapshot_lock.close()
        self.lock_file.close()

    def check_mount(self):
        if self.config.required_mount and not os.path.ismount(self.config.required_mount):
            raise StorageError("required snapshot mount is unavailable")

    def check(self, reserve=False):
        self.check_mount()
        p = private_directory(self.directory)
        st = p.stat()
        if (st.st_dev, st.st_ino) != self.identity:
            raise StorageError("snapshot directory was replaced or remounted; restart after inspection")
        if reserve and shutil.disk_usage(p).free < self.config.min_free_disk_gib * 2**30:
            raise StorageError("snapshot free-space reserve reached")

    def path(self, name):
        if not OWNED.fullmatch(name):
            raise StorageError("invalid owned filename")
        return self.directory / name

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            return value
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else ""

    def count(self, key, amount=1):
        with self.db:
            self.db.execute(
                "INSERT INTO counters VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                (key, amount),
            )

    def rows(self):
        return list(self.db.execute("SELECT * FROM snapshots WHERE complete=1 ORDER BY used DESC"))

    def reconcile(self):
        self.check()
        for row in list(self.db.execute("SELECT * FROM snapshots")):
            if row["complete"] == -1:
                self.remove(row["name"])
                continue
            if not row["complete"]:
                # A crashed HTTP operation may still be running on the server. Never delete underneath it.
                self.meta("blocked", "interrupted_snapshot_operation")
                continue
            try:
                s = self.path(row["name"]).lstat()
                good = stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and s.st_size == row["size"]
            except FileNotFoundError:
                good = False
            if not good:
                with self.db:
                    self.db.execute("DELETE FROM snapshots WHERE name=?", (row["name"],))
                self.count("stale_records")
        while len(self.rows()) > self.config.max_snapshots:
            self.remove(self.rows()[-1]["name"])
            self.count("evictions")

    def begin(self, identity, compatibility):
        self.check(reserve=True)
        name = f"ssp-{uuid.uuid4().hex}.bin"
        with self.db:
            self.db.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, 0, 0, '', ?, 0)",
                (name, json.dumps(identity.packed()), compatibility, time.time()),
            )
        return name

    def file_info(self, name):
        # Called off the event loop; hash uses bounded memory even for very large HDD snapshots.
        self.check()
        fd = os.open(self.path(name), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_mode & 0o077 or s.st_uid != os.getuid():
                raise StorageError("unsafe snapshot file ownership/type/permissions")
            h = hashlib.sha256()
            while block := f.read(4 * 1024 * 1024):
                h.update(block)
            return s.st_size, h.hexdigest()

    def commit(self, name, tokens, size, sha, protected=None):
        self.check()
        new = self.db.execute("SELECT * FROM snapshots WHERE name=?", (name,)).fetchone()
        identity = Identity.unpack(json.loads(new["identity"]))
        # Commit new ownership BEFORE deleting known-good files. Retired tombstones make
        # the filesystem/database boundary crash-recoverable without losing both copies.
        victims, remaining = [], []
        for row in self.rows():
            old = Identity.unpack(json.loads(row["identity"]))
            if (
                old.key == identity.key
                and row["compatibility"] == new["compatibility"]
                and row["name"] != protected
            ):
                victims.append(row)
            else:
                remaining.append(row)
        evictions = 0
        while len(remaining) >= self.config.max_snapshots:
            eligible = [r for r in remaining if r["name"] != protected]
            if not eligible:
                raise StorageError("all eviction candidates protected")
            victim = eligible[-1]
            remaining.remove(victim)
            victims.append(victim)
            evictions += 1
        with self.db:
            for victim in victims:
                self.db.execute("UPDATE snapshots SET complete=-1 WHERE name=?", (victim["name"],))
            self.db.execute(
                "UPDATE snapshots SET tokens=?, size=?, sha256=?, complete=1 WHERE name=?",
                (tokens, size, sha, name),
            )
        if evictions:
            self.count("evictions", evictions)
        for victim in victims:
            self.remove(victim["name"])

    def remove(self, name):
        self.check()
        # Only a catalog-owned name is eligible, never glob the directory.
        if not self.db.execute("SELECT 1 FROM snapshots WHERE name=?", (name,)).fetchone():
            return
        p = self.path(name)
        if p.is_symlink():
            raise StorageError("refusing snapshot symlink")
        p.unlink(missing_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        with self.db:
            self.db.execute("DELETE FROM snapshots WHERE name=?", (name,))

    def candidate(self, identity, compatibility):
        matches = [
            (identity.overlap(Identity.unpack(json.loads(r["identity"]))), r)
            for r in self.rows()
            if r["compatibility"] == compatibility
        ]
        matches = [(score, row) for score, row in matches if score]
        return max(matches, key=lambda pair: (pair[0], pair[1]["used"]))[1] if matches else None

    def touch(self, name):
        with self.db:
            self.db.execute("UPDATE snapshots SET used=? WHERE name=?", (time.time(), name))

    def report(self, estimates):
        rows = self.rows()
        retired = list(self.db.execute("SELECT size FROM snapshots WHERE complete=-1"))
        pending = self.db.execute("SELECT count(*) FROM snapshots WHERE complete=0").fetchone()[0]
        counters = dict(self.db.execute("SELECT key,value FROM counters"))
        tokens = counters.get("estimated_prompt_tokens_spared", 0)
        seconds = (
            tokens / estimates.prefill_tokens_per_second if estimates.prefill_tokens_per_second else None
        )
        kwh = (
            seconds * estimates.power_watts / 3_600_000
            if seconds is not None and estimates.power_watts
            else None
        )
        return {
            "blocked": self.meta("blocked") or None,
            "active": self.meta("active") or None,
            "context": json.loads(self.meta("context") or "null"),
            "snapshot_count": len(rows),
            "total_bytes": sum(r["size"] for r in rows) + sum(r["size"] for r in retired),
            "total_bytes_scope": "cataloged completed and retired files; excludes uncommitted/unrelated files",
            "retired_files_pending_cleanup": len(retired),
            "uncommitted_files": pending,
            "snapshots": [
                {"id": r["name"], "bytes": r["size"], "saved_tokens": r["tokens"], "last_used": r["used"]}
                for r in rows
            ],
            "counters": counters,
            "estimates_not_measurements": {
                "prompt_tokens_spared": tokens,
                "compute_seconds_avoided": seconds,
                "electricity_kwh_avoided": kwh,
                "electricity_cost_avoided": kwh * estimates.electricity_price_per_kwh
                if kwh is not None
                else None,
            },
        }
