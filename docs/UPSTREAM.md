# Strata source research and hard limits

Research target: public upstream commit
[`e88ee39d1d6be6e3364183af3b39b62a7ed6f211`](https://github.com/Niko1221/Strata/tree/e88ee39d1d6be6e3364183af3b39b62a7ed6f211).
This is source inspection, **not** a live-engine qualification. No private server was queried.
Recheck these contracts before using another Strata revision.

## Persistence contract

[`docs/DETAILS.md:1286–1409`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/docs/DETAILS.md#L1286-L1409)
and [`serve/server.py:3558–3629`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L3558-L3629):

* Enable `--slot-save-path DIR`, or server config `slot_save_path`. The server creates a missing
  directory with mode 0700; files are mode 0600. The proxy intentionally does **not** create
  its configured snapshot directory automatically.
* `POST /slots/0?action=save` or `restore`, JSON `{"filename":"plain-name.bin"}`.
  Response fields: `id_slot`, `filename`, `n_saved`/`n_restored`, `n_written`/`n_read`, and timings.
* Only slot 0, serial mode. Engine `--batch` is refused (even a batch engine with one slot).
  Server config `parallel: 1` or absent stays serial; values above 1 add `--batch`
  ([server.py:2905–2923](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L2905-L2923)).
  Engine session files also refuse `--layer-split`, `--peer-device`, and `--prompt-cache 0`.
  The in-RAM conversation cache need not be enabled.
* Authentication, Host validation, JSON content type, and trusted-Origin rules apply. Control
  calls use a configured server API credential, without impersonating a client's Origin.
* Operations run under the server FIFO, waiting for inference; they do not run during generation.
* No HTTP erase API and no file-download API. Files are local to **Strata**, not to the caller.
  Remote managed operation therefore requires a correctly mounted shared view of the same
  directory. A network-only backend without shared filesystem access supports pass-through,
  not this project's reliable disk retention. No SSH helper or invented remote-delete API exists.

## What is saved, and does a forwarded original request reuse it?

[`generate.cpp:11700–11920`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L11700-L11920)
saves running state, the live consumed token sequence, image identity, K/V layers, draft state,
and **only one selected checkpoint** (deepest, or deepest pinned if a pin exists;
[selection code](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/core/conversation_file.cpp#L1120-L1156)). `live.size()` is the authoritative `n_saved` value.

Generated tokens enter `consumed` during decode
([14361](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L14361)
and [14776](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L14776));
completed requests swap it into `live`
([14862–14879](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L14862-L14879)).
Thus snapshots include **consumed generated assistant tokens**, not merely the incoming prompt.
This does not mean every visible response token is guaranteed to be a cached cell, or that an
API assistant message can be reconstructed byte-for-byte from those cells.

The next request checks exact token/image prefixes against live state and checkpoints
([12201–12266](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L12201-L12266)).
The [documented cache rules](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/docs/DETAILS.md#L1095-L1121)
require exact tokens and pictures; changing a response, reasoning representation, template or
image can rewind reuse to an earlier checkpoint. Extending history can reuse the live state;
branching can reuse a matching checkpoint. Editing before the deepest saved checkpoint may
force substantial or complete re-prefill. Root checkpoints present in RAM are not all in the file.

**Yes:** restore followed by the **unaltered original request** is the intended protocol and can
avoid most prefill. **No:** restoration does not promise zero recomputation or deterministic
future output across process restarts. The upstream documentation describes a small tail
re-read and non-identical later output across restarts. The proxy never injects token IDs,
trims messages, reconstructs assistant messages, or tells Strata to trust its hashes.

`strata_checkpoint:false` explicitly opts out of persistent conversation state
([DETAILS:1107–1111](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/docs/DETAILS.md#L1107-L1111));
these requests pass through without an identity.

## Token accounting: an unavoidable lower bound

The serial [`GET /slots` implementation](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6121-L6139)
returns `n_ctx`, `is_processing`, and the last `status.prompt_tokens` as `n_prompt_tokens`.
It is **not** the current `live.size()` and does not include generated assistant tokens.
Save/restore clears the status prompt count. The batch engine has different `held_tokens`
reporting but does not support session files.

[`GET /v1/status`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L4180-L4225)
reports model, version, server start time, context, concurrency, activity counters and last timings.
[`cache_n`, `prompt_n`, `predicted_n`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5100-L5118)
distinguish reused prompt tokens, freshly read prompt tokens, and generated tokens. Adding these
is not a documented exact idle-KV measurement, especially after internal tool/recovery rounds.

Eligibility therefore uses the last successfully correlated **prompt count / n_ctx** as an
accurate lower bound, at `>= threshold`. It never uses characters or writes a snapshot just to
measure its size. The saved file's token count is recorded after successful save. A conversation
whose prompt is just below threshold but whose generated tail crosses it may not be saved.
Exact combined-context thresholding is **not implementable through the inspected serial HTTP
API** without extra engine instrumentation. This is an intentional limitation, not a claim of
exact live KV utilization.

## Compatibility, integrity, publication, errors

[DETAILS:1351–1409](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/docs/DETAILS.md#L1351-L1409):

* Versioned native Strata format, not llama.cpp sessions. Contains conversation token IDs:
  treat files as sensitive private data.
* Model fingerprint samples file sizes and first/last MiB. Same-size edits in the middle can
  escape detection. Engine version is fingerprinted, **not a binary build hash**.
* Config binding covers KV layout, device backend, max context, RoPE, steering/control vectors,
  arithmetic settings and other state-interpretation settings; sampling generally is not binding.
* Restore validates file integrity, bounds, geometry and fingerprints before device writes.
  Invalid/corrupt/incompatible refusal leaves current state unchanged. A transfer failure after
  device writes begins is fatal: the engine exits rather than decode partial state.
* Save uses an exclusive temporary file, flush, rename, and directory flush. `error.published`
  indicates a failed post-rename durability step. The proxy always requests a **new UUID name**,
  so a failed replacement never overwrites an earlier cataloged copy.
* Statuses include 400 invalid, 404 missing, 501 disabled/parallel, 503 not loaded/memory,
  507 storage, and 500 I/O or engine failure. A structured `error.kind` matters more than status
  alone. The proxy never retries ambiguous mutations.
* `--session-min-free-mib` performs a backend full-file-plus-reserve disk preflight. Set it to
  at least the desired reserve; the proxy's filesystem check cannot predict exact file size.
* Linux can use O_DIRECT with buffered fallback; ordinary HDD/SSD filesystems are supported.
  Directory-fsync limitations weaken power-loss durability. Host RAM floors also apply;
  optional `--session-save-reclaim` trades RAM caches for save admission, not required here.

The proxy requires a **whole saved input prefix**, not merely partial message overlap, before
nominating a disk snapshot. The HTTP API does not expose which token/image checkpoint that file
contains. This conservatively forgoes some possible pinned/rewind hits; see the [history-rewrite
investigation](COMPACTION.md) for compaction behavior, bounded zero-reuse suppression and context accounting.

The proxy additionally partitions by an operator-managed `compatibility_key`, engine/model/context
and prompt-affecting request fields, then checks a full-file SHA-256 before restore. Change the
key whenever model contents, tokenizer/template, engine build, or relevant config changes.
Never mutate model files under retained snapshots. The key is an operational assertion, not
remote cryptographic attestation.

## Ownership and restart limits

Strata's FIFO serializes each request, not a sequence of HTTP calls. There is no compare-and-swap
slot generation or exclusive ownership lease exposed by these APIs. Status reads are not atomic
with save/restore. Versioned telemetry has a server instance identity, but does not provide an
atomic session ownership API either. The current implementation uses `/v1/status` start/version/
model/context and request counters plus `/slots` counts to invalidate stale active associations.
It cannot reliably distinguish every child-engine restart, same-second server restart, settings
change or malicious out-of-band race. **Exclusive backend access is a prerequisite, not optional
hardening.** Use one proxy worker and enforce firewall/process access rules. Status drift checks
are mitigation, not a distributed lock. Existing APIs and the backend's own exact-prefix checks
remain authoritative; if these guarantees are insufficient, disable snapshot management.

## Endpoint safety audit

The focused review reused the pinned public source above; no live endpoint was queried. “Read-only”
here means **does not mutate inference-slot/KV state**, not that a metrics counter or HTTP log can
never change. Client authentication, bodies, responses and errors still pass through unchanged.

| Method / paths | Evidence and policy |
|---|---|
| GET `/health`, `/api/health`, `/v1/models`, `/models`, `/props`, `/slots`, `/v1/status`, `/status` | Existing bypass. [GET dispatch](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6068-L6144) returns status/configuration views, not inference. |
| GET `/metrics`, `/metrics/prometheus`, `/v1/telemetry` | Bypass. [Metric/telemetry handlers](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5840-L5869) read counters/status; Prometheus sampling may update telemetry accounting, not the inference slot. |
| GET `/api/requests`, `/settings`, `/config`, `/mcp` | Bypass. [GET handlers](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6039-L6060) read retained records/defaults/config/tool status. [`_config_get`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6441-L6450) reads the config file; it does not load/reconfigure the engine. |
| GET `/`, `/api-monitor`, `/fonts/*`, `/web/*`, `/v1/images/files/*` | Bypass. [Fonts/web assets](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5805-L5839), [HTML and generated-image files](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6061-L6085) are file reads (or errors), not generation. |
| HEAD on the same GET allowlist | Bypass retained. The inspected handler does not implement `do_HEAD`; the standard unsupported-method response is non-mutating. The proxy does not synthesize GET or promise HEAD success. |
| POST `/v1/messages/count_tokens`, `/v1/v1/messages/count_tokens` | Bypass. [`_count_tokens`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6810-L6817) renders/tokenizes with `store=False`, never `svc.load()` or `prepare()`/generation. [`encode_prompt`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L4243-L4257) explicitly does not add/replace kept prompts. The doubled-v1 [alias](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L246) is supported upstream. |
| OPTIONS, any path | Bypass. [`do_OPTIONS`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L5756-L5761) is a global CORS 204 handler, with no per-path state-changing dispatch. |
| POST chat/messages/responses, image generation, slots save/restore, load/unload, VRAM reserve, settings/config changes | **Gated**, including unknown/unidentified bodies. [POST dispatch](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/serve/server.py#L6152-L6277) contains inference, lifecycle/control, and settings mutations. GET exemptions never extend to these methods. |
| All other methods/routes | **Gated** and forwarded unchanged. No blanket GET exemption or guessed `/tokenize`/`/detokenize` exemption. A 404 in this revision does not establish a stable slot-read-only contract for unknown routes. |

Classification mirrors Strata's trailing-slash stripping and ignores the query for routing; it does
not decode percent escapes or rewrite the forwarded target. Method/path decisions are internal;
clients supply no new headers. Reaudit this allowlist when changing the supported Strata revision.

## Integrity-check I/O review

No integrity behavior was removed or weakened. `Store.file_info()` sequentially reads and hashes
the complete file in bounded 4 MiB chunks, once after each save and once before each restore.
For a file of size **S**, one save followed by one restore adds **2S logical read bytes** beyond
Strata's own S-byte write and S-byte restore read. N restores add `(1 + N) × S` proxy read bytes
in total. On a cold-cache HDD this can materially increase disk work; shared storage can add
network traffic too. OS caching may reduce actual physical reads, so no latency/energy measurement
is inferred from those byte counts. Ordinary continuations and read-only requests hash no files.

Strata already validates header and payload checksums, fingerprints, bounds, and end marker in
[`conversation_file.cpp:1188–1255`](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/core/conversation_file.cpp#L1188-L1255).
The engine then validates the whole state before device transfer
([generate.cpp:11876–11910](https://github.com/Niko1221/Strata/blob/e88ee39d1d6be6e3364183af3b39b62a7ed6f211/src/program/generate.cpp#L11876-L11910)).
This is usable defense against corrupt/incompatible files, but the HTTP acknowledgement exposes
counts/sizes/timings, **not an authoritative digest**. The proxy's SHA-256 additionally binds its
catalog entry to the exact bytes visible through the configured filesystem, detecting even a
replacement with a different otherwise-valid Strata session file. Size/mtime-only validation cannot
preserve that guarantee; parsing the native file format would add coupling without avoiding a
full payload check. Therefore the conservative full reads remain, with no new tuning knob or
integrity bypass. Actual HDD/energy effects still need separately approved measurement.
