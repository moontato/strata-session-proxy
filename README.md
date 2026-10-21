# Strata Session Proxy

A transparent HTTP reverse proxy that preserves expensive Strata inference sessions on disk
when conversations switch. The objective is to avoid redundant long-context computation and
its electricity use—not to replace Strata, translate APIs, or integrate with a particular client.

```text
Any Strata-compatible HTTP client → this proxy → Strata
```

Clients change only their endpoint address. Methods, paths, query parameters, authentication,
request bodies, status codes, errors, tool calls, reasoning and multimodal content pass through.
Responses stream incrementally, including SSE. Unknown endpoints still forward. Snapshot
management is **off by default**, and disabled mode makes no management calls or state files.

**Status:** working Python implementation, tested against a synthetic local HTTP backend.
No live Strata server or GPU was used. Source contracts were researched at a pinned upstream
commit; hardware/backend qualification is still required. See [upstream evidence and hard
limits](docs/UPSTREAM.md), not just the feature list, before enabling managed mode.

## What it does

* Serializes state-changing traffic in managed mode; audited read-only views/static files,
  token-count POSTs and CORS preflights bypass the gate without snapshot or metadata writes.
  Unknown routes stay gated. The gate covers the entire stream and backend idle confirmation.
* Matches exact, incrementally hashed message-history prefixes. Shared system prompts alone
  never match. Histories, tools, media identity, relevant options and authentication scope matter.
* Saves only when switching away from a successfully correlated, eligible session. Divergent
  branches, rewinds, replays and ambiguous response boundaries are switches—not continuations
  merely because they share a prefix. Strict full-input-history extensions with normal assistant
  response/user-or-tool boundaries avoid snapshot writes. Small SQLite bookkeeping writes are separate.
* Restores only candidates whose **whole saved input history** is a prefix of the new input,
  then forwards the **original request**. Shared earlier messages or moved recent messages do
  not suffice: disk snapshots keep one selected checkpoint, not every shared boundary. Strata
  decides token/image compatibility; fingerprints never override its checks.
* Compacted/rewritten histories become new active inputs. Ordinary continuations then avoid
  snapshot I/O. Confirmed zero-reuse file/input pairs are suppressed in bounded memory for
  10 minutes; absent telemetry is unknown, not zero. See [compaction findings and upgrade](docs/COMPACTION.md).
* Uses private SQLite metadata, unique filenames, validated publication, SHA-256 integrity,
  LRU retention, filesystem free-space checks, mount guards and a local-only status/recovery CLI.
* Fails closed on an uncertain save/restore, preserving a durable recovery marker. Ordinary
  inference continues after clearly non-mutating refusals or preflight storage unavailability.

## Important limitations

1. **Single serial Strata engine only:** no engine `--batch`, server `parallel > 1`, layer split
   or peer device. Server `parallel: 1` (or omitted) is serial. Prompt caching must be enabled.
   Strata's in-RAM conversation cache is optional.
2. **Exclusive backend access:** all inference and control traffic must go through one proxy
   process. No API can make separate HTTP operations atomic against bypass clients. Do not
   deploy multiple workers or point two catalogs at one snapshot directory.
3. **Shared filesystem required for remote snapshots:** `directory` must expose the same files
   as Strata's `slot_save_path`. Network-only access is sufficient for ordinary forwarding,
   but not reliable retention because Strata offers no remote-delete API.
4. **Threshold uses a prompt-token lower bound**, not exact post-generation KV occupancy.
   Serial `/slots` does not report the latter. At 30% of a 262,144 context, 20K/60K prompt tokens
   are ineligible and 100K/210K are eligible. Generated tokens can push actual occupancy higher.
5. Automatic detection supports JSON `messages` histories on `/v1/chat/completions`,
   `/chat/completions`, and `/v1/messages`. Other APIs—including stateful `/v1/responses`, raw
   completions, compressed or oversized requests, and mutable external image references—still
   forward, but do not acquire a reusable identity. This is conservative fallback, not an API adapter.
6. Identical histories cannot reveal whether two clients privately regard them as different
   conversations. A complete saved input can nominate a checkpoint even if assistant output
   later differs. Strata must validate tokens/images. Speculative partial-prefix snapshots,
   including some useful pinned/rewind cases not exposed by HTTP, are deliberately not restored.
   Active fingerprints cover incoming histories, not generated output. A newly appended assistant
   message is assumed to represent the last response; edits solely to that previously unobserved
   output cannot be detected without response reconstruction. See [continuation boundaries](docs/ARCHITECTURE.md#continuation-is-not-candidate-overlap).
7. HTTP/1.1 origin-form request/response forwarding, not a general TCP tunnel: WebSocket upgrades,
   CONNECT, HTTP/2 end-to-end, interim-response replay and HTTP trailers are not implemented.
   Hop-by-hop headers and transfer framing are regenerated; body bytes, not wire chunk boundaries,
   are preserved. Backend Host replaces client Host, and redirects are passed through unmodified.
8. Snapshots do not guarantee identical future generated output across engine restarts.
   Energy figures are estimates, not measurements or proof of incremental savings.

## Linux installation (no Docker, GPU or systemd needed)

Python **3.11+**, `venv`, and pip are required. Install these with your distribution's package
manager (Debian/Ubuntu, Fedora, Arch, Alpine, etc.). Dependencies are aiohttp and PyYAML;
Linux x86-64/ARM64 work where Python/dependency wheels or build prerequisites are available.
Only x86-64 Python 3.11 was exercised here.

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install .
cp config.example.yaml config.local.yaml
chmod 600 config.local.yaml
.venv/bin/strata-session-proxy --config config.local.yaml check-config
```

`check-config` makes **no network requests**. Edit the ignored local file to set backend and
listen addresses. Begin with `snapshots.enabled: false`. Starting the proxy itself does not
probe the backend; client requests cause forwarding (and, in managed mode, status/control calls).

```sh
.venv/bin/strata-session-proxy --config config.local.yaml serve
```

Stop with Ctrl-C or SIGTERM. Shutdown waits for in-flight work; do not force-kill a snapshot
operation. See recovery below if a timeout or kill is unavoidable.

Point clients at `http://127.0.0.1:8082` instead of the backend origin. Keep the same API suffix
(e.g. a client's base URL may be `http://127.0.0.1:8082/v1`), request schema and credentials.
There are no proxy-specific client headers, session IDs or plugins.

### Enable snapshots: operator procedure

**These steps affect your deployment. They are instructions for the operator, not actions this
project performs automatically. Schedule any Strata changes when its current work is safe.**

1. Read [UPSTREAM.md](docs/UPSTREAM.md). Confirm your Strata version supports the documented
   session API and compatible serial configuration. Preserve your original config for rollback.
2. Choose private snapshot and metadata directories **outside the source checkout**. Run the
   proxy as the same Unix identity that owns Strata's 0600 files. Do not make snapshots world-
   or group-readable to avoid aligning ownership. On shared storage, align numeric ownership.
   For the example paths, provision them as the intended service user:
   ```sh
   install -d -m 700 /var/lib/strata-session-proxy/snapshots
   install -d -m 700 /var/lib/strata-session-proxy/state
   ```
   Administrative provisioning/ownership changes may be needed; do not run the proxy as root.
3. If using a mount, mount it first and set `required_mount` to the exact mountpoint. Do not let
   Strata auto-create its save directory under an absent mount either; enforce the same mount
   prerequisite on the Strata service. Use a local filesystem for the SQLite state directory.
4. Configure Strata's server with `--slot-save-path /var/lib/strata-session-proxy/snapshots`
   (or `slot_save_path` in its config). Keep `--prompt-cache` positive. Set server `parallel: 1`
   or omit it; do **not** pass engine `--batch` (even 1), layer-split or peer-device options.
   In the engine arguments, set
   `--session-min-free-mib 10240` for the example 10 GiB reserve. This is an example, not a
   hardware recommendation. Apply/restart Strata yourself during an approved maintenance window.
5. Enforce exclusive backend access using binding/firewall/process controls; ensure no client
   bypasses the proxy. Set `exclusive_backend: true` only once that is true. Keep one proxy process.
6. Set `compatibility_key` to your own non-secret deployment revision. Change it whenever model
   bytes, tokenizer/template, engine build, KV/context or other relevant settings change. Do not
   reuse old keys across incompatible deployments. Strata's own fingerprint checks remain required.
7. Set `enabled: true`, the desired threshold/retention, and the shared directory path as visible
   to the proxy. If backend authentication is enabled, provide its credential through the
   environment variable named by `admin_token_env` (default `STRATA_PROXY_BACKEND_TOKEN`) using
   your private environment/secret manager. Do not put credentials in URLs or committed YAML.
   Client Authorization/x-api-key headers are forwarded independently and never replaced.
8. Validate locally, start the proxy, and arrange an explicitly approved **synthetic** live
   qualification in an idle maintenance window. Suggested checks are in [TESTING.md](docs/TESTING.md).

### Private network / remote backend

Set `server.listen_host` to your chosen private-interface address in **ignored local config**.
Set `backend_url` to Strata's reachable origin, which can be on localhost or another machine.
Use your own private VPN hostname/address; no real deployment address belongs in this repository.
Do not bind to all interfaces unless you intend that exposure. Use firewall rules, backend API
authentication and TLS termination or a trusted encrypted network. TLS backend verification is on;
there is no insecure certificate-bypass option. Client HTTPS termination is external to this app.

For remote snapshots, the two configured directory paths can differ but must refer to the same
files on shared storage, with coherent visibility, rename and flush behavior. Keep SQLite local.
Set an explicit mount guard. NFS failure modes and distributed-filesystem power-loss durability
are **not qualified**. The simplest deployment is proxy and Strata on the same host.

## Administration and recovery

```sh
.venv/bin/strata-session-proxy --config config.local.yaml status
```

This reads the private local SQLite catalog; it never contacts Strata. It shows active opaque
fingerprint, last observed prompt utilization, snapshot count/bytes, counters, and labeled energy
estimates. It is a recorded observation, not a live poll. There is no administrative network route.

If save/restore timed out, the engine may still be working. **Do not simply retry it.** The proxy
returns 503 for subsequent state-changing traffic until recovery. Read-only endpoints still forward.

1. Stop the proxy; wait for outstanding backend work to finish. Inspect Strata locally. If state
   cannot be confirmed safe, arrange an operator-authorized backend restart; the proxy never does it.
2. Verify the correct mount, free space, private ownership, backend configuration and operation
   completion. Keep known-good snapshots; do not remove files while Strata is writing/reading them.
3. Only after confirming no pending backend operation, run offline:
   ```sh
   .venv/bin/strata-session-proxy --config config.local.yaml recover --acknowledge-backend-quiescent
   ```
   Recovery takes the process lock, removes only catalog-owned incomplete files, clears the
   ambiguity marker, and makes **no backend requests**. It trusts your acknowledgement.
4. Restart the proxy. Active identity is always forgotten across restarts; completed compatible
   snapshots can still be found. Missing/stale catalog entries are reconciled at startup. Unrelated
   files and untracked upstream temporary files are never glob-deleted. See [storage details](docs/ARCHITECTURE.md).

## Optional systemd

[deploy/strata-session-proxy.service](deploy/strata-session-proxy.service) is an editable example.
Install the package into its example `/opt/strata-session-proxy/venv`, place private configuration
under `/etc/strata-session-proxy/`, set `User`/`Group` to the **actual Strata file owner**, and adjust
paths and mount dependencies before installing it. The example user name is generic, not an
instruction to change Strata's user. Never publish the edited deployment unit or environment file.

```sh
# After reviewing/editing the service and provisioning its configured directories:
sudo cp deploy/strata-session-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now strata-session-proxy
# Stop safely:
sudo systemctl stop strata-session-proxy
```

systemd is optional; the foreground command works on other Linux init systems.

## Tests, documentation and privacy

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

Tests bind loopback ephemeral ports and use synthetic messages and snapshot files only. They do
not require Strata, CUDA, a GPU, network credentials, or your live endpoint.

* [Architecture, safety, failure behavior, storage](docs/ARCHITECTURE.md)
* [Configuration reference](docs/CONFIGURATION.md)
* [Upstream evidence / unsupported capabilities](docs/UPSTREAM.md)
* [History rewrites, compaction investigation, safe upgrade](docs/COMPACTION.md)
* [Testing and optional live qualification](docs/TESTING.md)
* [Troubleshooting](docs/TROUBLESHOOTING.md)
* [Security and repository hygiene](SECURITY.md)

### Rollback

Stop new client submissions and let the proxy finish active inference/snapshot work. Stop the
proxy. Restore clients' original backend endpoint, or restart with `snapshots.enabled: false`
for pure forwarding. If a snapshot operation was interrupted, confirm backend quiescence first.
No Strata executable or inference logic was modified. Removing `slot_save_path` later is optional
and an operator-controlled change. Keep or securely remove private snapshot files only while
neither service is accessing them; retain the catalog if you want reuse after re-enabling.

MIT licensed. No publishing remote is configured by this project.
