# Troubleshooting without exposing private data

Start with the **local** `check-config` and `status` commands. Neither contacts Strata. Do not
paste raw server responses, snapshot files, SQLite files, credentials, deployment addresses or
actual prompts into a public issue. Reproduce with synthetic inputs wherever possible.

| Symptom | Checks / response |
|---|---|
| Startup configuration error | Check the named YAML setting, type, range and environment override. Managed mode needs an explicit compatibility key and exclusive-backend acknowledgement. |
| Startup storage error | Directory must already exist, be real (not symlink), mode 0700, owned by the runtime UID. Check both filesystem mounts and ownership; do not broaden permissions. |
| Another proxy owns storage | Stop the duplicate process. Do not delete a lock file while a process holds it. Lock files can remain after normal shutdown; the advisory lock, not file existence, controls ownership. |
| 502 upstream unavailable | Verify operator-supplied backend origin and routing locally. Backend requests require permission before live troubleshooting; the proxy will not discover addresses or restart Strata. |
| Streaming stalls | Check client/frontend buffering and timeout configuration. `read_timeout_seconds: 0` permits long prefill and SSE idle gaps. A managed request also waits behind earlier inference or disk operations. |
| No snapshots | Verify managed mode, `auto_save`, eligible prompt utilization, reliable history detection, actual conversation switching and compatible Strata snapshot configuration. No save after every message is expected. |
| `status_unavailable` grows | Control authentication, unsupported status schema, unloaded model or non-serial backend. The proxy forwards without guessing snapshot ownership. |
| `failed_saves` grows | Check free disk space/reserve, RAM admission, permissions, same-file visibility and Strata session support. `--batch` even with one slot is unsupported. Upstream 501 is a safe refusal, not a save. |
| Disk reserve reached | Free space on the configured filesystem or explicitly adjust reserve/retention after reviewing capacity. Never delete unrelated files or a file under active save/restore. The backend needs full-new-file-plus-reserve space; lowering only the proxy floor does not override it. |
| Large HDD delays | Increase the operation deadline and stop timeout after estimating file size/speed. Hash validation adds sequential reads. Slow storage is supported; switching cannot safely overlap inference. |
| No restore despite matching-looking text | Compare synthetic histories for exact prefixes, system/tool content, model/options, auth scope, extension headers and inline image identity. Approximate similarity is intentionally ignored. Mutable URLs, unsupported APIs and oversized/compressed inputs pass through without an identity. |
| Restore but no reuse | Check `restore_without_reported_hit` (confirmed zero) versus `restore_reuse_unknown` (missing/inconsistent telemetry). A successful RESTORE is a file transfer, not proof of useful KV reuse. Zero-reuse file/input pairs are suppressed before hashing for 10 minutes, at most 128 process-local entries; `restore_suppressed` counts skips. Templates, reasoning/tool serialization and image identity still affect Strata's checks. |
| Frequent rewrites/compaction and snapshot churn | See [the compaction investigation](COMPACTION.md). Moved recent messages do not match as suffixes. Disk candidates require the entire saved input prefix, not arbitrary shared earlier messages. A current-input replay may still save its generated tail but no longer restores an older copy over itself. Repeated frontend compaction is not conclusively explained by snapshot logs; do not alter context limits or hide usage values. |
| Snapshot unexpectedly evicted | Examine max count and LRU recency. Returning snapshots are protected while saving; with a one-entry limit the outgoing save is skipped. Incompatible old-key entries are retained until normal eviction. |
| `external_changes` grows | A bypass client, restart or backend setting change may have invalidated active ownership. Enforce exclusive access; polling cannot close an ownership race. |
| Mount disappeared | Do not recreate the snapshot directory. Restore the correct mount on both hosts, inspect pending operations, and restart the proxy after its directory identity changes. |
| 503 operator recovery required | Follow the README's recovery procedure. An HTTP timeout does not prove a remote disk operation stopped. Confirm quiescence before clearing the durable marker. |
| CLI has no catalog in disabled mode | Expected: pure forwarding does not create runtime state. `status` requires an existing managed-mode catalog. |
| Catalog damaged or lost | Stop the proxy. Keep private files for inspection/backup; do not guess conversation associations or auto-import them. Restore a consistent catalog/files backup, or use a new empty private snapshot directory and accept re-prefill. Never silently discard a catalog to bypass an uncertain operation. |

## Safely reporting a bug

Include software/Python versions, configuration **key names** involved, fixed error categories,
synthetic reproduction steps and aggregate counts. Omit paths/addresses and any values that identify
your deployment. The project logs no upstream error body by default; inspecting such a body on your
own server is an operator action, and it must remain private. Tests provide a local mock reproducer
without requiring any live-server access.
