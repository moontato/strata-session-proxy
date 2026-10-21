# Testing and qualification

## Automated mock suite

Verified in the development container after the focused history-rewrite correction: **166 tests passed**, including a run with warnings treated
as errors, on Linux x86-64 / Python 3.11 with aiohttp 3.14.5. Ruff lint/format checks, bytecode
compilation and dependency consistency checks passed. A wheel was built and installed into a
separate clean virtual environment; its CLI validated the generic example without network access.
These results are mock/proxy qualification, not real Strata or hardware qualification.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

Only synthetic data is used. The mock implements Strata's inspected serial HTTP shapes, FIFO,
last-prompt counts, request counters, generated-state bookkeeping, save/restore acknowledgements,
private disk files, timing delays and failure injection. It is **not an inference engine** and
cannot validate numerical KV correctness. Its snapshot representation is synthetic JSON, not
Strata's binary format. All listeners bind `127.0.0.1` on OS-assigned ports. An autouse fixture
rejects external socket connections and DNS lookups; tests never load a deployment configuration.

### Coverage map

| Requested behavior | Coverage |
|---|---|
| Basic transparent HTTP / arbitrary paths, methods, queries, errors | `test_forwarding.py`: direct-vs-proxy body bytes, binary bodies, encoded paths/query, error statuses, HEAD, duplicate response headers |
| Incremental SSE | First event received while backend waits on an explicit gate, before final events are permitted; no whole-response buffering |
| Tools, reasoning, multimodal | Synthetic nested payload forwarded byte-for-byte, including whitespace and inline media identity |
| Same conversation / no unnecessary writes | No snapshot actions on repeated/extended history, including generation-parameter changes |
| Threshold below / exactly at / above | Parameterized 200/299/300/400/900 tokens against 1,000-token context and 30% threshold |
| Switch and restore | Ordered save/restore/inference events; restored-prefix cache counter |
| Exact prefix / shared systems / branches | `test_identity_storage_config.py` and `test_branch_switches.py`: directional continuation vs symmetric candidate overlap; synthetic 180K shared + 20K/15K sibling branches, A saved before B and restored on return, multiple siblings, short/system-only prefixes, replays, rewinds, ambiguous boundaries and normal tool/streaming continuations |
| Client independence | Ordinary requests without custom headers, both supported API dialects; User-Agent excluded from identity |
| LRU / count limit | LRU recency touch, eviction, count limit, lowered limit behavior in reconciliation code; requested incoming snapshot protected at limits 1 and 2 |
| Disk full / unavailable storage | Mock disk-usage floor, backend 507, renamed/missing snapshot directory, mount prerequisite |
| Corrupt/incompatible state | Same-size file tampering, digest rejection, engine/model refusal path, compatibility-key/build partitioning |
| Backend restart | Start/counter drift invalidates active association; compatible stored snapshot can restore |
| Proxy restart | Resource/catalog teardown and new application using existing state; no inferred active identity; retained candidates reused |
| Concurrent inference | Concurrent client requests with serialized mock events and artificial inference/save delays |
| Interrupted streams / failed saves | Client disconnect, cancelled request during save, safe published/I/O refusal, ambiguous timeout circuit breaker |
| Generic Linux config | YAML/env precedence, strict types/ranges, origin validation, private directory/lock checks, local CLI |
| HDD-like behavior | Artificial slow save/restore; first SSE event and read-only monitoring remain independently observable |
| Disabled mode | No control calls or state directory; request-body streaming without parsing |
| Harmless endpoint policy | `test_endpoint_policy.py`: method-specific status/config/static/token-count/OPTIONS bypass; active identity and SQLite change count unchanged, including during SSE; unknown/mutating/encoded lookalike paths remain gated and byte-preserving |
| Integrity overhead unchanged | Exactly one full-file hash per save and one before restore; no hash on normal continuations; existing corruption/failed-restore tests retained |
| Stale/out-of-band slot | Direct synthetic bypass inference invalidates prior association; unknown proxy mutation drops active identity |
| Crash bookkeeping | Incomplete operation marker persists; committed replacement survives deletion failure; tombstone reconciliation; unrelated files retained |
| HTTP protocol level | Raw HTTP/1.1 socket with chunked upload, exact decoded payload, encoded path/query; hop-by-hop filtering; absolute-target rejection prevents upstream-origin override |
| Privacy and estimates | Sanitized error messages, local-only status, no payload/model disclosure in catalog report, configured estimate formulas |

The long-sibling regression was also run against a scratch copy of the original coordinator:
both streaming and nonstreaming variants failed at the expected missing-save assertion
(`inference, inference` instead of `inference, save, inference`). The corrected implementation
passes those regressions. The large token counts are synthetic backend accounting, not actual
180K-token inference or a physical large-KV benchmark.

`test_compaction.py` adds 21 cases: synthetic 230K/234K histories rewritten into 10K–30K
summary/recent-message inputs, unchanged system prompts and repositioned recent tool/reasoning/
inline-media content, normal subsequent turns, a second rewrite/replay, genuinely reusable small
saved roots and older branches, zero-reuse suppression/expiry/history scope, malformed/missing/
inconsistent telemetry, small positive hits, bounded negative memory, suppressed-candidate fallback,
active replay, context accounting and unchanged direct/proxied usage, threshold and LRU.
The same pre-fix-source run reproduced **4 failures** (weak-prefix streaming/nonstreaming restores,
repeated zero-reuse HDD reads, and active replay restores), while **2 summary-first compaction
variants already passed**. This distinction is important: moved suffix matching was ruled out,
not invented as the deployed root cause. See [the investigation](COMPACTION.md).

The tests exercise conservative behavior—not perfect detection of unobservable external changes.
The source/API limitations in [UPSTREAM.md](UPSTREAM.md) still apply.

## Developer checks

```sh
.venv/bin/python -m compileall -q src
.venv/bin/strata-session-proxy --config config.example.yaml check-config
# Optional development formatter/linter, not a runtime dependency:
.venv/bin/pip install ruff
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

Use `TMPDIR` or an explicitly chosen private test directory for artifacts. Pytest temporary files
are synthetic but still should not enter Git. Package-install testing can use a wheel built with
`pip wheel --no-deps . --wheel-dir <private-test-directory>` and a separate virtual environment.
No CI job or script in this project invokes an actual Strata server.

## What has not been qualified

* Actual binary Strata session files, GPU/device transfers, output quality/parity, context-scale
  memory demand, or real prefill savings. Source inspection cannot substitute for these checks.
* A physical HDD, SSD power-loss recovery, NFS coherence/failure, bind-mount edge cases, or
  sudden power loss during a filesystem/SQLite transaction. Tests simulate delays and failures.
* ARM64 or other distributions/Python versions not exercised in the development environment.
* Real systemd startup/shutdown and mount ordering on the deployment host.
* A force-killed production process with an actual remote engine operation still running. The
  durable marker and staged-file tests cover the recovery policy, not every OS scheduling race.
* General HTTP tunnels, WebSocket upgrades, HTTP/2 transport, trailers or replay of interim 1xx
  responses. These are outside the implemented HTTP/1.1 request/response scope.

## Optional live qualification — explicit approval required

**Do not run this against someone else's active server without their explicit approval for that
specific operation.** No live endpoint is included or discovered by the tests. Do not infer
permission for later operations from an earlier approval. Installing/configuring a proxy does
not authorize probing the backend.

Suggested separate approvals, in order:

1. **Read-only capability check:** approve exactly one authenticated `GET /v1/status` and one
   `GET /slots` against the chosen server. Inspect only version, serial concurrency, loaded state,
   context and idleness. Do not publish raw responses (they may include deployment hardware).
   This does not prove `slot_save_path` is enabled; inspect operator-controlled config locally.
2. **Maintenance/configuration window:** operator confirms no important work is active and
   explicitly authorizes any necessary Strata configuration/restart. Enable supported serial
   snapshot mode, reserve, ownership and mount guards. The proxy itself never changes Strata.
3. **One bounded synthetic switch sequence:** agree on prompt sizes/output caps and maximum
   disk/compute cost first. Approve synthetic A inference, synthetic B switch (save A), return
   to extended A (save B and restore A), and the required status reads. Expect these operations
   to mutate/replace the active slot and write potentially large files. Verify client payload
   preservation, `n_saved`, sizes, cache_n and correctness of the returned synthetic task.
4. **Separate recovery/restart tests:** approve each restart, intentional cancellation or corrupt
   *disposable test snapshot* test separately. Never corrupt/delete an existing real snapshot.
5. **Cleanup:** approve removal of the specifically identified test snapshot files only after all
   operations finish. Keep real runtime artifacts outside the repository. Publish aggregate
   redacted counts, not prompts, responses, addresses, credentials, files or raw server logs.

To measure electricity rather than estimate it, a further approved counterfactual workload with
power measurements is needed. A `cache_n` counter alone does not prove incremental net energy savings.
