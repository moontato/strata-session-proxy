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
and **only the deepest checkpoint**. `live.size()` is the authoritative `n_saved` value.

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
