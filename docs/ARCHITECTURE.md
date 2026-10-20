# Architecture and safety model

## Modules

| Module | Responsibility |
|---|---|
| `proxy.py` | aiohttp reverse forwarding, bounded request inspection, streaming, hop-by-hop filtering, cancellation |
| `identity.py` | canonical JSON message-boundary SHA-256 chains and exact candidate matching |
| `backend.py` | documented status, slots, save and restore HTTP calls |
| `coordinator.py` | single-slot lock, switch policy, eligibility, ambiguity circuit breaker |
| `storage.py` | private SQLite catalog, file ownership, integrity, mount guards, LRU, counters |
| `config.py`, `cli.py` | strict YAML/env configuration and local-only administration |

There are two runtime dependencies: aiohttp and PyYAML. SQLite, file locks, hashing and disk
checks are standard library functionality. No model, GPU runtime, CUDA, client SDK or agent
framework is imported.

## Forwarding and request identification

Disabled mode streams every body without parsing and creates no catalog. Managed mode inspects
only supported JSON history requests up to `detection_max_bytes` in memory. Larger declared
bodies stream directly; chunked bodies crossing the cap retain only the bounded initial chunks
and stream the rest. Request bytes are never reserialized or spooled to disk. No response parser
is required: generated reasoning/tool output is not reconstructed or stored.

The HTTP client disables decompression, cookies, automatic redirects and environment proxies.
Duplicate end-to-end headers (including Set-Cookie) survive. Host is the backend's authority;
Connection-nominated fields and standard hop-by-hop fields are removed in both directions.
No client authentication is replaced with the administrative credential. The proxy is an origin
reverse proxy, never an arbitrary-target forward proxy. Streaming output uses backpressure and
never waits for the entire response. Transfer coding and header casing/order can change.

History canonicalization sorts JSON object keys, preserves arrays, strings, roles and every
message field, and rejects duplicate keys/non-finite values. Each complete message extends a
length-framed SHA-256 chain. Scope includes API path, authentication digests, body fields other
than explicitly known generation-only controls, and query/extension-header digests. A user
message is required; a shared system/developer prompt alone is insufficient. Stored state contains
only digests, not raw history. An earlier full input prefix can match a longer history or a rewind;
a shared later message boundary can nominate a branch. Longest matching candidate wins, with
recency as tiebreaker. Hashes select a candidate **only**—Strata's exact token/image-prefix checks
are the final authority, especially when an assistant response was edited.

Two clients sending identical histories cannot be distinguished without client cooperation.
Independent conversations sharing only system prompts do not match. Exact histories sharing
actual user content may nominate the same state, which is safe only because Strata checks the
real prompt before reuse. Mutable external image/file references fall back; inline data identity
is hashed. Unknown APIs, bodies over the cap, compressed inputs, invalid JSON and
`strata_checkpoint:false` do not acquire identities but still forward normally.

## Coordination state machine

Managed mode has one asyncio lock and process-level `flock` leases on both the catalog and
snapshot directories. The asyncio lock spans:

1. Observe idle using `/v1/status` + `/slots`. Check single serial slot, model/version/context,
   server start value, activity counter, last prompt count. Missing/unsupported status means
   no snapshot operations and ordinary forwarding, not guessed accounting.
2. Compare observation with the prior correlated result. Any drift forgets active identity.
   On a compatible active-history continuation, perform no save/restore.
3. On a switch, locate/protect the incoming candidate. If outgoing prompt utilization reaches
   the threshold, request save to a fresh UUID name, validate its success and local file,
   then commit/retain it. Do not save after each message or on shutdown.
4. Validate a candidate's full SHA-256; restore it. Clear refusals (invalid/missing or explicit
   memory refusal) can fall back to ordinary inference. Ambiguous failures block new mutations.
5. Forward the original request, including the entire streaming lifetime. After EOF, confirm
   backend idle. Only a successful, identified request with unchanged boot/config and exactly
   one additional completed activity count becomes the new active identity. Internal multi-round
   behavior that cannot be correlated conservatively loses cache eligibility.
6. Record prompt utilization, hashes and counters. Later calls validate this observation again.

Known read-only GET/HEAD monitoring routes bypass the lock. Unknown routes, including unknown
GET endpoints, enter the gate because future endpoints might mutate state. Unsupported requests
invalidate active identity. This may reduce hit rate; it cannot teach the proxy a nonexistent API.

A separate coordinator task survives HTTP handler cancellation. It finishes any in-flight snapshot
command, cancels ordinary forwarding on client disconnect, and waits for backend idle before
releasing ownership. A forced process kill cannot roll back a remote command. Before each snapshot
mutation, a durable `blocked` marker is written. A crash/timeout leaves the marker in place; startup
refuses state-changing traffic until offline operator recovery. Unexpected management failures also
latch the in-memory gate closed, including metadata write failures. A response already sent cannot
be retroactively replaced; no fake SSE error is appended, and subsequent mutations fail clearly.

## Boundaries that cannot be solved in a transparent proxy

The upstream API has no transaction or slot ownership token. Two status reads can race another
process. The operator **must** enforce exclusive access to one backend. `exclusive_backend` is an
acknowledgement, not a magical lock over the network. Drift detection catches many out-of-band
inferences/restarts but not every same-count change or child-process restart. Model compatibility
requires an operator revision key in addition to upstream validation. Never run multiple proxy
workers, multiple catalogs sharing a snapshot directory, or bypass writers.

Single-slot prompt count is a lower bound on live KV occupancy. No exact combined-KV metric is
invented. A save acknowledgement supplies actual saved tokens only **after** eligibility is decided.
Any client-visible schema can still pass through even if it cannot be optimized.

## Storage, LRU and recovery

* Snapshot directory must already exist, be owner-only, and be owned by the proxy/Strata UID.
  The proxy checks the configured exact mountpoint (when set), directory device/inode and permissions
  before operations. It never recreates a disappeared snapshot directory. Startup checks catch
  missing mounts before opening state; runtime checks prevent silent remount/fallback use.
* Mount checks run on the proxy host. They cannot stop the remote Strata host from writing under
  its own missing mount, or eliminate an unmount race between checking and opening. Configure
  mount dependencies/permissions on **both** hosts. No software disk check is a space reservation.
* SQLite state is local and private; transaction mode is DELETE journal, `synchronous=FULL`.
  Process locks reject a second writer to either the same catalog or snapshot directory (subject
  to the filesystem's lock semantics). Do not put this database on NFS.
* Names are `ssp-<random UUID>.bin`, stored in the catalog. Only catalog-owned names may be
  deleted. No directory-wide cleanup pattern, erase-all action or untrusted user filename is used.
* Before save, check free space on the actual snapshot filesystem. Strata's engine must enforce
  full-new-file-plus-reserve admission with `--session-min-free-mib`; set that independently to
  the desired reserve. Keep headroom for **one additional full snapshot** during replacement,
  plus the engine's transient hidden temporary file publication behavior.
* New files remain staged until a successful acknowledgement, safe regular-file/owner/mode/link
  checks, reported-size agreement and full SHA-256 read. One SQLite transaction publishes the
  new completed entry and marks prior copies/eligible LRU victims as retired. Only **after that
  commit** are retired files deleted. A deletion failure preserves the new committed file and a
  retired tombstone for startup cleanup; it cannot destroy both copies. Files and SQLite cannot
  form one atomic cross-process transaction: a crash before that commit can leave a validated
  staged file and a recovery marker. Recovery deliberately sacrifices an uncommitted staged copy
  rather than guessing it was committed. Retired files may temporarily consume extra space until
  cleanup succeeds, but are never eligible restore candidates.
* Completed catalog entries never exceed `max_snapshots`. A staged replacement may temporarily
  occupy additional space. Restore targets are protected during outgoing saves; with a limit of
  one, saving the outgoing session is skipped to preserve the requested target. Successful restore
  updates LRU recency. No user-pinned entries are implemented.
* Full-file hashing adds sequential read I/O after saving and before restoring. It uses a worker
  thread and bounded memory, preserving responsiveness of read-only forwarding on slow HDDs.
  Snapshot operations remain serialized; speed is never a safety assumption. SQLite/fsync/mount
  metadata operations are small synchronous calls and may stall on an unhealthy filesystem.
* Startup drops records for missing/unsafe/size-changed completed files, enforces a lowered count
  limit, and latches incomplete operations. Unexpected untracked files—including Strata's own
  hidden temporary files after a crash—are left untouched for operator inspection. Back up the
  catalog with the files; losing the catalog loses trusted identities/ownership, so files are
  not auto-imported. A corrupt catalog causes startup failure, never silent recreation.
* Neither encrypted storage nor secure erase is provided. Snapshots contain private token IDs/KV.
  Use disk encryption and appropriate filesystem/backup retention if required.

## Failure policy

| Failure | Behavior |
|---|---|
| Backend unreachable before forwarding | sanitized 502; no guessed snapshot |
| Status unsupported/missing before request | drop active identity, ordinary forwarding |
| Threshold not reached / saving disabled | no save |
| Preflight reserve reached / mount missing | skip new snapshot operation; normal inference can continue |
| Explicit non-mutating save refusal | keep prior good snapshots; remove owned staging file |
| Local digest mismatch | remove owned corrupt candidate; ordinary processing |
| Invalid/incompatible/missing restore refusal | retire candidate, ordinary processing |
| Explicit restore memory refusal | keep candidate for future use; ordinary processing |
| Timeout, malformed control response, fatal transfer, uncertain save | persist block; no automatic mutation retry |
| Cannot confirm idle after correlated request/cancellation | block future mutations; never overlap unsafe restore |
| Catalog I/O failure | fail closed in memory; durable pre-operation marker protects uncertain mutations on restart |
| Proxy restart | forget active identity; reconcile catalog; preserve completed compatible snapshots |
| Backend drift/restart | forget active association; retained compatible candidates still require validation |

## Statistics and privacy

No access logs or prompt/response/error-body logs by default. Operational events use only opaque
fingerprints, random snapshot names and fixed error categories. Local `status` reads metadata without
probing Strata. All state is sensitive even though histories are hashes: fingerprints can leak equality
and may support guessing if the catalog is disclosed. Directory mode 0700 is mandatory.

A successful restore followed by reported `last_timings.cache_n` contributes
`min(cache_n, prompt_tokens)` to **estimated prompt tokens spared**. This is real backend reuse
but only an estimate of attribution to disk restoration: some RAM checkpoint reuse could have
occurred anyway. `cache_hits` means reported reuse after restore, not mathematical incremental
savings. Without observations it does not manufacture hits.

With configured prefill throughput `R` (tokens/s), power `P` (watts), and price `C` (currency/kWh):

```text
estimated seconds = estimated tokens / R
estimated kWh     = estimated seconds × P / 3,600,000
estimated cost    = estimated kWh × C
```

Zero throughput/power leaves the corresponding estimate unavailable. Estimates exclude snapshot
I/O energy, CPU/network overhead, idle power, context-dependent prefill scaling and cache reuse
that would have occurred anyway. They are **not net energy measurements**. Use externally measured
counterfactual workloads and a power meter for defensible energy claims.
