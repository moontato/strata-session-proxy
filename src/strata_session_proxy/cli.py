"""Local-only administration; no HTTP admin listener and no implicit backend probes."""

import argparse
import json
import logging
import os
import sqlite3

from aiohttp import web

from .config import load
from .proxy import create_app
from .storage import StorageError, Store, private_directory


def main():
    parser = argparse.ArgumentParser(description="Transparent Strata session proxy")
    parser.add_argument("--config", help="YAML path (or STRATA_PROXY_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("check-config", help="validate configuration without network access")
    sub.add_parser("status", help="read local metadata; never contacts Strata")
    recovery = sub.add_parser("recover", help="offline recovery; proxy must be stopped")
    recovery.add_argument("--acknowledge-backend-quiescent", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        try:
            config = load(args.config)
        except ValueError as e:
            # Config validation messages name settings, never their private values.
            parser.exit(2, f"configuration error: {e}\n")
        if args.command == "check-config":
            print("configuration valid (no network requests made)")
        elif args.command == "serve":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            # aiohttp's default access log contains paths/queries; deliberately disabled.
            web.run_app(
                create_app(config),
                host=config.server.listen_host,
                port=config.server.listen_port,
                access_log=None,
                print=None,
                handler_cancellation=True,
                shutdown_timeout=config.server.shutdown_timeout_seconds,
            )
        elif args.command == "status":
            path = private_directory(config.snapshots.state_directory) / "catalog.sqlite3"
            if path.is_symlink():
                raise ValueError("refusing symlink catalog")
            store = object.__new__(Store)
            store.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            store.db.row_factory = sqlite3.Row
            try:
                print(json.dumps(store.report(config.estimates), indent=2))
            finally:
                store.db.close()
        else:
            if not args.acknowledge_backend_quiescent:
                raise ValueError(
                    "first stop the proxy and confirm Strata has no pending operation; acknowledgement required"
                )
            store = Store(config.snapshots)
            try:
                for row in list(store.db.execute("SELECT name FROM snapshots WHERE complete=0")):
                    store.remove(row["name"])
                store.meta("blocked", "")
                store.meta("active", "")
                store.meta("context", "null")
            finally:
                store.close()
            print("local ambiguity marker cleared; no backend requests made")
    except StorageError as e:
        parser.exit(2, f"storage error: {e}\n")
    except (ValueError, OSError, sqlite3.Error) as e:
        # Configuration parser exceptions may contain private values: don't echo them.
        parser.exit(2, f"error: {type(e).__name__}; check configuration and private storage permissions\n")


if __name__ == "__main__":
    main()
