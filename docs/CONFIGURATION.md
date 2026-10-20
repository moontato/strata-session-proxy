# Configuration reference

All settings are optional and shown in `config.example.yaml`. Unknown keys, wrong types,
non-finite numbers, unsafe origins and invalid ranges fail validation. No configuration loader
or `check-config` request contacts Strata. Example values are generic, never machine assumptions.

Precedence, lowest to highest:

1. Dataclass defaults.
2. Explicit `--config PATH`, otherwise `STRATA_PROXY_CONFIG` (no implicit config discovery).
3. `STRATA_PROXY__SECTION__KEY` environment variables, parsed as YAML scalar values.

For example, set `STRATA_PROXY__SNAPSHOTS__ENABLED=false` to disable managed mode. Copy the
example to ignored `config.local.yaml` for local overrides; config is not hot-reloaded. Restart
the proxy gracefully after changes. Do not echo a resolved config into public bug reports.

## `server`

| Setting | Default | Meaning |
|---|---|---|
| `listen_host` | `127.0.0.1` | Local bind address. Set a private-interface address locally for remote clients. |
| `listen_port` | `8082` | Integer port 1–65535. |
| `backend_url` | `http://127.0.0.1:8081` | HTTP(S) origin only; no userinfo, query, fragment or path prefix. |
| `connect_timeout_seconds` | `10` | Positive connection timeout; also total deadline for small control status reads. |
| `read_timeout_seconds` | `0` | Ordinary upstream socket-read inactivity timeout. Zero is unlimited; no overall inference deadline. |
| `shutdown_timeout_seconds` | `3600` | aiohttp graceful handler shutdown deadline; remote snapshot completion can require additional time. |
| `detection_max_bytes` | `8388608` | Maximum inspected history body; larger bodies still stream and forward. Zero effectively disables JSON identification. |

Both listening and outbound ordinary HTTP use HTTP/1.1. HTTPS backend verification is enabled.
For incoming TLS, use a trusted TLS frontend that does not buffer SSE. No environment HTTP proxy
is implicitly used. Payload limits/rate limits and client TLS authentication belong to deployment
policy, not an undocumented API conversion here.

## `snapshots`

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | False: pure forwarding, no status calls, snapshot commands or catalog. |
| `exclusive_backend` | `false` | Operator assertion of exclusive access; required true in managed mode. |
| `compatibility_key` | empty | Required nonempty deployment revision in managed mode; change with model/tokenizer/template/build/KV/config changes. Never a credential. |
| `directory` | `/var/lib/strata-session-proxy/snapshots` | Existing absolute, owner-only directory exposing Strata's actual snapshot files. |
| `state_directory` | `/var/lib/strata-session-proxy/state` | Separate absolute private local catalog directory; created if absent. |
| `required_mount` | empty | Optional exact mountpoint that must contain `directory`; checked at startup and each file operation. Set for external/mounted storage. |
| `save_threshold_percent` | `30` | Number 0–100, eligible at or above this **prompt-count lower bound**. Zero makes every correlated nonempty prompt eligible. |
| `auto_save` | `true` | False stops new automatic saves but permits reuse of existing candidates. |
| `max_snapshots` | `5` | Positive integer limit on completed cataloged snapshots. Staged replacement needs extra capacity. |
| `eviction_policy` | `lru` | Only LRU is supported. Successful restores refresh recency. |
| `min_free_disk_gib` | `10` | Nonnegative reserve checked against the snapshot filesystem. Set matching backend `--session-min-free-mib` for full-file admission. |
| `operation_timeout_seconds` | `7200` | Positive total HTTP deadline for save/restore; choose generously for HDDs and large contexts. Expiry is ambiguous and blocks further mutations. |
| `idle_timeout_seconds` | `120` | Positive deadline to observe quiescent backend around requests/cancellation. |
| `poll_interval_seconds` | `0.1` | Positive interval only while waiting for idle. No background polling loop. |
| `admin_token_env` | `STRATA_PROXY_BACKEND_TOKEN` | Name of an environment variable holding Strata's administrative API token. If unset, control requests send no Authorization. |

The token is sent as Bearer only to the configured backend for control calls. It is neither
logged nor inserted into ordinary client traffic. Do not place its value in YAML or Git. An
ignored environment file with mode 0600, a service manager credential mechanism or an existing
secret manager can populate the variable.

## `estimates`

All default to zero; values must be finite and nonnegative.

| Setting | Meaning |
|---|---|
| `power_watts` | Assumed incremental prefill power, not a discovered GPU specification. Zero = unavailable energy estimate. |
| `prefill_tokens_per_second` | Assumed prefill throughput; zero = unavailable time/energy estimates. |
| `electricity_price_per_kwh` | Optional price in the operator's currency. Zero = no cost assigned. |

The CLI labels the entire calculation `estimates_not_measurements`. See architecture for attribution
limits, formulas, and excluded costs. No power sensors are accessed.
